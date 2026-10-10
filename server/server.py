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
import homestead_version as RELEASE
from contextlib import nullcontext
from functools import wraps

# Imported ahead of the feature modules because settings are read during start.
import homestead_names as NAMES
import homestead_app_settings as APP_SETTINGS
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
HOMESTEAD_VERSION = os.environ.get("HOMESTEAD_VERSION", RELEASE.VERSION)
_self_data_fence = None
_self_data_barrier = None
_self_data_boot_pending = False
_self_data_boot_failed = False

DEFAULT_APP_SETTINGS = APP_SETTINGS.DEFAULTS

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

# Each background task says when it last did its work, and what went wrong
# if it did not, so Settings › About can say whether Homestead is healthy
# rather than only whether it answers.
HEART = {}
_heart_lock = threading.Lock()


def _heartbeats():
    """Each background task's last beat, copied under the lock."""
    with _heart_lock:
        return {k: dict(v) for k, v in HEART.items()}


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
        CHANGES.note_write(method, path, body)
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
        return APP_SETTINGS.validate(raw)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return APP_SETTINGS.validate({})
        raise
    except (ValueError, TypeError, json.JSONDecodeError):
        return APP_SETTINGS.validate({})


def save_app_settings(value):
    settings = APP_SETTINGS.validate(value)
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


# Looked up when asked, so a test that patches either is the one the route calls.
APP_SETTINGS.bind(lambda: app_settings_payload(), lambda value: save_app_settings(value))


# ---------------------------------------------------------------- collectors
def _hardware_loop():
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    NODES.reconcile_hardware()
                    PORTS.observe(NODES.node_temps())
                beat("hardware", 30, leader_only=True)
            except Exception as error:
                beat("hardware", 30, error, leader_only=True)
                print(f"hardware: {str(error)[:160]}", flush=True)
        time.sleep(30)


def _uptime_loop():
    """Every minute on the leader: does each running app answer at its address."""
    while True:
        started = time.time()
        if LEADER.is_leader():
            try:
                UPTIME.observe(cached("wl", 5, WORKLOADS.get_workloads), vms=cached("vms", 5, VMS.list_vms))
                try:
                    outage_tick()
                except Exception as error:
                    print(f"outage actions: {str(error)[:160]}", flush=True)
                beat("uptime", UPTIME.CHECK_EVERY, leader_only=True)
            except Exception as error:
                beat("uptime", UPTIME.CHECK_EVERY, error, leader_only=True)
                print(f"uptime: {str(error)[:160]}", flush=True)
        time.sleep(max(5, UPTIME.CHECK_EVERY - (time.time() - started)))


def _unifi_loop():
    """Hourly on the leader, while UniFi is connected: its clients and names
    as they are now, rather than as they were when someone last pressed Sync."""
    time.sleep(120)
    while True:
        if LEADER.is_leader():
            try:
                if IPAM.sync_due():
                    IPAM.sync_unifi()
                beat("unifi", 600, leader_only=True)
            except Exception as error:
                # The failure is recorded on the card (last_error); the loop carries on.
                beat("unifi", 600, leader_only=True)
                print(f"unifi: {str(error)[:160]}", flush=True)
        time.sleep(600)


def _forecast_loop():
    """Hourly on the leader: today's usage of each volume, disk and the pool."""
    while True:
        if LEADER.is_leader():
            try:
                FORECAST.observe(cached("vol", 8, STORAGE.get_volumes), LHCAP.status())
                beat("forecast", 3600, leader_only=True)
            except Exception as error:
                beat("forecast", 3600, error, leader_only=True)
                print(f"forecast: {str(error)[:160]}", flush=True)
        time.sleep(3600)


def _changes_loop():
    """Every minute on the leader: what changed in each app since the last look."""
    last = time.time()
    while True:
        started = time.time()
        if LEADER.is_leader():
            try:
                deps = [d for d in kget("/apis/apps/v1/deployments").get("items", [])
                        if d["metadata"]["namespace"] not in SYS_NS]
                CHANGES.observe(deps, now=started, since=last)
                beat("changes", 60, leader_only=True)
            except Exception as error:
                beat("changes", 60, error, leader_only=True)
                print(f"changes: {str(error)[:160]}", flush=True)
        last = started
        time.sleep(max(5, 60 - (time.time() - started)))


def change_undo_plan(body):
    """Undo one change: the app's settings from before it, reviewed like any rollout."""
    ns, name = _dns_name(body.get("ns"), "namespace"), _dns_name(body.get("name"), "workload name")
    guard_managed_smb(ns, name)
    if is_self(ns, name):
        raise ValueError("Homestead's own settings are changed from Settings, not undone here")
    entry = CHANGES.kept_before(ns, name, str(body.get("id") or ""))
    current = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    proposed, rows = CHANGES.undo_plan(current, entry)
    missing = sorted({(v.get("persistentVolumeClaim") or {}).get("claimName")
                      for v in proposed["spec"]["template"]["spec"].get("volumes") or []
                      if (v.get("persistentVolumeClaim") or {}).get("claimName")}
                     - {p["metadata"]["name"] for p in kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims").get("items", [])})
    if missing:
        raise ValueError(f"those settings used {', '.join(missing)}, which no longer exist{'s' if len(missing) == 1 else ''}")
    context = {"action": "change-undo", **rollout_review_context(current), "proposed_spec": proposed["spec"], "entry": entry["id"]}
    cache = {f"/apis/apps/v1/namespaces/{ns}/deployments/{name}": current}

    def read(path):
        if path not in cache:
            cache[path] = kget(path)
        return copy.deepcopy(cache[path])

    def planner(*args, **kwargs):
        return PLACE.manifest_plan(*args, **kwargs, read=read)
    capacity = ROLLOUT_CAPACITY.plan(current, proposed, ns, read, PLACE.get_nodes, planner,
                                     get_app_settings()["thresholds"]["memory"]["critical"])
    capacity["requires_confirmation"] = True
    return current, proposed, rows, capacity, context


def logsearch_read(ns, pod, container, since, limit):
    """One container's log lines of the last since seconds, with timestamps."""
    query = f"sinceSeconds={int(since)}&limitBytes={int(limit)}&timestamps=true" + (
        f"&container={urllib.parse.quote(container)}" if container else "")
    req = urllib.request.Request(f"{API}/api/v1/namespaces/{urllib.parse.quote(ns)}/pods/{urllib.parse.quote(pod)}/log?{query}",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=CTX, timeout=20) as r:
        return r.read(int(limit) + 1).decode("utf-8", "replace")


def _housekeeping_loop():
    """Once a day on the leader, ten minutes after start: keep the data volume in bounds."""
    time.sleep(600)
    while True:
        if LEADER.is_leader():
            try:
                HOUSEKEEPING.tidy_unused()
                beat("housekeeping", 86400, leader_only=True)
            except Exception as error:
                beat("housekeeping", 86400, error, leader_only=True)
                print(f"housekeeping: {str(error)[:160]}", flush=True)
        time.sleep(86400)


def set_vm_monitoring(b):
    """How a VM is monitored: automatic, a port, a web page on a port, or off.
    An annotation on the VM, so it does not restart."""
    ns, name = _dns_name(b.get("ns"), "namespace"), _dns_name(b.get("name"), "VM name")
    mode = b.get("mode")
    # Automatic on a VM tries the usual ports; a port belongs to the other two.
    value = UPTIME.check_setting(mode, b.get("path"), b.get("port") if mode in ("tcp", "http") else None)
    if value in ("tcp",) or (value and value.startswith("/")):
        raise ValueError("choose the port to ask on this VM")
    ksend("PATCH", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}",
          {"metadata": {"annotations": {NAMES.key("uptime"): value or None}}}, ctype="application/merge-patch+json")
    _cache.pop("vms", None)
    words = {"": "checked automatically", "off": "not monitored"}
    return {"ok": True, "detail": f"{name} is {words.get(value, f'checked at {value}')}"}


def set_schedule(b):
    """Stop and start an app or VM on a schedule, or no longer. An annotation,
    so nothing restarts."""
    kind = b.get("kind")
    ns = _dns_name(b.get("ns"), "namespace")
    if kind == "app":
        name = _dns_name(b.get("name"), "workload name")
        guard_managed_smb(ns, name)
        require_workload_target(ns, name)
        if is_self(ns, name):
            raise ValueError("Homestead cannot be put on a schedule: it is what runs the schedules")
        path = f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"
    elif kind == "vm":
        name = _dns_name(b.get("name"), "VM name")
        path = f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}"
    else:
        raise ValueError("a schedule is for an app or a VM")
    schedule = SCHEDULES.clean(b.get("schedule"))
    ksend("PATCH", path, {"metadata": {"annotations": {
        NAMES.key(SCHEDULES.KEY): json.dumps(schedule, separators=(",", ":")) if schedule else None}}},
        ctype="application/merge-patch+json")
    _cache.pop("wl" if kind == "app" else "vms", None)
    return {"ok": True, "schedule": schedule,
            "detail": f"{name}: {SCHEDULES.describe(schedule).lower()}" if schedule else f"{name} is no longer on a schedule"}


def outage_tick():
    """After a monitoring round, on the leader: any outage action that is due."""
    found = OUTAGE.items(cached("wl", 5, WORKLOADS.get_workloads), cached("vms", 5, VMS.list_vms))
    if not found:
        return []
    operations = OPS.list_operations()
    working = {((o.get("resource") or {}).get("namespace"), (o.get("resource") or {}).get("name"))
               for o in operations if o.get("status") in ("queued", "running")}

    def restart(item):
        if item["kind"] == "app":
            restart_workload(item["ns"], item["name"])
        else:
            api_vm_power(item["ns"], item["name"], "restart")
        _cache.pop("wl" if item["kind"] == "app" else "vms", None)

    return OUTAGE.tick(found, UPTIME.report(), restart, lambda item: (item["ns"], item["name"]) in working, operations)


def scheduled_items():
    return SCHEDULES.items(cached("wl", 5, WORKLOADS.get_workloads), cached("vms", 5, VMS.list_vms))


def schedule_act(item, action):
    """Do a scheduled stop or start as the buttons do, with their checks."""
    ns, name = item["ns"], item["name"]
    if item["kind"] == "app":
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        current = int((dep.get("spec") or {}).get("replicas") or 0)
        if action == "stop":
            if not current:
                return f"{name} was already stopped"
            ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
                  {"metadata": {"annotations": {NAMES.key(SCHEDULES.REPLICAS_KEY): str(current)}}},
                  ctype="application/merge-patch+json")
            api_scale(ns, name, 0)
            return f"stopped {name}"
        if current:
            return f"{name} was already running"
        try:
            copies = int(NAMES.read((dep.get("metadata") or {}).get("annotations") or {}, SCHEDULES.REPLICAS_KEY) or 1)
        except ValueError:
            copies = 1
        try:
            api_scale(ns, name, max(1, min(copies, 100)))
        except API_V1.ApiError as error:
            raise ValueError(error.message) from None
        return f"started {name}" + (f" with {copies} copies" if copies > 1 else "")
    if action == "stop":
        if not item["running"]:
            return f"{name} was already stopped"
        api_vm_power(ns, name, "stop")
        return f"shutting {name} down"
    if item["running"]:
        return f"{name} was already running"
    try:
        api_vm_power(ns, name, "start")
    except API_V1.ApiError as error:
        raise ValueError(error.message) from None
    return f"starting {name}"


def _schedules_loop():
    """Every minute on the leader: any scheduled stop or start that is due.
    Its writes are noted as the schedule's, so change history leaves them out."""
    CHANGES.REQUEST.user = CHANGES.BY_SCHEDULE
    while True:
        started = time.time()
        if LEADER.is_leader():
            try:
                if SCHEDULES.tick(scheduled_items(), schedule_act):
                    _cache.pop("wl", None)
                    _cache.pop("vms", None)
                beat("schedules", 60, leader_only=True)
            except Exception as error:
                beat("schedules", 60, error, leader_only=True)
                print(f"schedules: {str(error)[:160]}", flush=True)
        time.sleep(max(5, 60 - (time.time() - started)))


def set_uptime_setting(b):
    """An app's own check: auto, an HTTP path, TCP only, or off - on any of its
    published ports, or one chosen. An annotation, so nothing restarts."""
    ns, name = _dns_name(b.get("ns"), "namespace"), _dns_name(b.get("name"), "workload name")
    port = b.get("port") if b.get("mode") != "off" else None
    if port not in (None, ""):
        w = next((w for w in cached("wl", 5, WORKLOADS.get_workloads) if w["ns"] == ns and w["name"] == name), None)
        published = {int(p["port"]) for p in (w or {}).get("ports") or [] if p.get("port")}
        if w is not None and str(port).isdigit() and int(port) not in published:
            raise ValueError(f"{name} does not publish port {port}; choose one of "
                             + (", ".join(str(p) for p in sorted(published)) or "its ports"))
    value = UPTIME.check_setting(b.get("mode"), b.get("path"), port)
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
          {"metadata": {"annotations": {NAMES.key("uptime"): value or None}}}, ctype="application/merge-patch+json")
    _cache.pop("wl", None)
    return {"ok": True, "value": value, "detail": f"{name} is {UPTIME.describe_setting(value)}"}


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
            raise WORKLOADS.WorkloadRefused("not enough eligible capacity for the requested replicas", plan)
        if plan["requires_confirmation"] and not confirm_capacity:
            raise WORKLOADS.WorkloadRefused("review node memory before starting", plan)
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


def set_vm_logos(b):
    """Gives VMs a logo: an image (kept like a workload's), one of the OS
    logos, or neither - back to the OS the VM reports. Only the VM's own
    annotations change, so it does not restart."""
    items = b.get("items") or []
    if not items:
        raise ValueError("choose at least one VM")
    if len(items) > 200:
        raise ValueError("at most 200 VMs at a time")
    plans = []
    for item in items:
        ns, name = _dns_name(item.get("ns"), "namespace"), _dns_name(item.get("name"), "VM name")
        source, os_key = str(item.get("icon") or "").strip(), str(item.get("os") or "").strip()
        if source and not source.startswith(("https://", "http://")):
            raise ValueError("a logo is a public http:// or https:// address")
        if os_key and os_key not in LOGOS.OS_KEYS:
            raise ValueError(f"there is no OS logo {os_key[:40]}")
        plans.append((ns, name, source, os_key))
    done, failed = 0, []
    for ns, name, source, os_key in plans:
        try:
            cached_ref = ICONS.persist(source, DATA_DIR) if source else ""
            ksend("PATCH", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}",
                  {"metadata": {"annotations": {NAMES.key("icon"): cached_ref or None,
                                                NAMES.key("icon-source"): source or None,
                                                NAMES.key("logo-os"): os_key or None}}},
                  ctype="application/merge-patch+json")
            done += 1
        except Exception as e:
            failed.append(f"{name}: {e}")
    _cache.pop("vms", None)
    detail = (f"{done} logo{'s' if done != 1 else ''} saved" if any(s or o for *_, s, o in plans)
              else "back to its OS's logo") + (f"; {len(failed)} could not be: " + "; ".join(failed[:3]) if failed else "")
    return {"ok": not failed, "done": done, "failed": failed, "detail": detail}


def portal_status(force=False):
    """Each portal link's dot. A link to an app takes the app's own Monitoring
    state - asked every minute, over HTTP, down only after three misses - so
    the portal and Containers agree; any other link is a TCP connection."""
    status = dict(PORTAL.status(force=force))
    try:
        answers, links = UPTIME.report(), PORTAL.stored()
    except Exception:
        return status
    for link in links:
        icon = str(link.get("icon") or "")
        if not icon.startswith("workload:"):
            continue
        a = answers.get(icon.split(":", 1)[1])
        if not a or a.get("state") not in ("up", "slow", "down"):
            continue
        last = a.get("last") or {}
        status[link["id"]] = {"up": a["state"] != "down", "ms": last.get("ms"), "state": a["state"], "source": "monitoring",
                              "since": a.get("since"), "error": last.get("error") or "", "code": last.get("code")}
    return status


def helm_chart_logos():
    """{(namespace, release): {name, icon}} for every Helm release whose chart names a logo."""
    try:
        rows = cached("helm-logos", 60, HELM.releases)
    except Exception:
        return {}
    return {(r["namespace"], r["name"]): {"name": r.get("chart") or r["name"], "icon": r.get("icon", "")}
            for r in rows if r.get("icon")}


def logo_choices(ns, name, term, kind="workload"):
    """The picker's tiles for one workload: its chart's logo and its image's
    match, then a search. A VM has no image, so it is the search alone."""
    workload = None if kind == "vm" else next(
        (w for w in cached("wl", 5, WORKLOADS.get_workloads) if w.get("ns") == ns and w.get("name") == name), None)
    images = (workload or {}).get("images") or []
    chart = helm_chart_logos().get((ns, (workload or {}).get("helm_release"))) if workload else None
    return {"tiles": LOGOS.suggest(DEPLOY.fetch_appstore(), images, term if term is not None else name, DEPLOY.search_appstore,
                                   chart=chart), "images": images}


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


def get_overview():
    nodes = NODES.get_nodes()
    wl = WORKLOADS.get_workloads()
    vols = STORAGE.get_volumes()
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
    health = CLUSTER.classify_cluster_health(nodes, wl, vols, protection=protection)
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
        item = ensure_profile_compatible(DEPLOY.analyze_deploy_intent(item))
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


import homestead_share_servers as SHARE_SERVERS
SMB_NAME = SHARE_SERVERS.SMB_NAME


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


def capacity_manifest(config, existing=None):
    """Construct a read-only manifest and planned claims without provisioning."""
    cfg = DEPLOY.analyze_deploy_intent(copy.deepcopy(config))
    ns = _dns_name(cfg.get("namespace") or DEFAULT_NS, "namespace")
    cfg["namespace"] = ns
    joining = cfg.get("target_mode") == "existing"
    if joining:
        target = _dns_name(cfg.get("target_workload"), "existing workload")
        if existing is None:
            existing = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{target}")
        dep, _ = DEPLOY.build_sidecar_deployment(cfg, existing)
    elif cfg.get("target_mode", "new") == "new":
        dep, _ = DEPLOY.build_deployment(cfg)
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
    b = ensure_profile_compatible(DEPLOY.analyze_deploy_intent(copy.deepcopy(b)))
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
    return DEPLOY.run_deploy(b, reviewed_current=current) if current is not None else DEPLOY.run_deploy(b)


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
        cfg = ensure_profile_compatible(DEPLOY.analyze_deploy_intent(copy.deepcopy(rows[name]["config"])))
        guard_managed_smb(cfg.get("namespace") or DEFAULT_NS, cfg.get("workload_name") or cfg.get("name"))
        cfg, dep, pending, _ = capacity_manifest(cfg)
        for claim, descriptor in pending.items():
            if claim in claims and claims[claim] != descriptor:
                raise ValueError("inconsistent shared PVC settings: " + claim)
            claims[claim] = descriptor
        configs.append(cfg)
        entries.append({"name": name, "deployment": dep, "replicas": dep["spec"]["replicas"]})
    return configs, entries, claims


def compose_preview(b):
    configs, entries, claims = compose_preparation(b)
    HOSTACCESS.require_cfg(configs)
    return {"capacity": DEPLOY.compose_capacity(entries, claims),
            "capacity_token": CAPACITY_REVIEW.issue(b, {"action": "compose", "configs": configs})}


def compose_apply(b):
    """Guard the entire batch before any writes, then recheck its remainder."""
    configs, entries, claims = compose_preparation(b)
    HOSTACCESS.require_cfg(configs)
    plan = DEPLOY.compose_capacity(entries, claims)
    if plan["status"] == "unknown":
        raise CAPACITY_REVIEW.Rejected("Complete batch placement could not be verified; split the batch and review again", plan)
    CAPACITY_REVIEW.enforce(b, plan, {"action": "compose", "configs": configs})
    created, controllers = [], {}
    for cfg, entry in zip(configs, entries):
        name = entry["name"]
        try:
            if created:
                fresh = DEPLOY.compose_capacity(entries, claims, controllers)
                if fresh["blocked"]:
                    return {"ok": False, "created": created, "failed": name,
                            "error": "Remaining batch no longer fits the checked capacity; review again", "capacity": fresh}
            result = DEPLOY.run_deploy(copy.deepcopy(cfg))
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


import homestead_storage_classes as STORAGE_CLASSES
import homestead_storage as STORAGE


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
            "vm_network_options": NETWORK.vm_network_options(NODES.node_temps()),
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
# carries it on from another replica (homestead_power_jobs).
POD_NAME = os.environ.get("HOSTNAME", "")
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


def operation_item(operation_id):
    """A job-tray item as it stands now, or None."""
    return next((item for item in OPS.list_operations() if item.get("id") == operation_id), None) if operation_id else None


def own_node():
    """The node this Homestead pod runs on."""
    try:
        return kget(f"/api/v1/namespaces/{SELF.NS}/pods/{os.environ.get('HOSTNAME', '')}")["spec"]["nodeName"]
    except Exception:
        return ""


def api_vm_power(ns, name, action):
    """Start, stop or restart a VM for /api/v1: stop as the app does; start and
    restart through the same capacity review, taken as acknowledged when it
    is not blocked - the key's scope is the consent the app asks a person for.
    A start that needs a person (missing TPM or EFI state) is refused."""
    if action not in ("start", "stop", "restart"):
        raise ValueError("start, stop or restart")
    body = {"ns": _dns_name(ns, "namespace"), "name": _dns_name(name, "VM name"), "action": action}
    if action == "stop":
        result = VM_REVIEW.reviewed_vm_power(body)
        _cache.pop("vms", None)
        return {"warnings": [], "job": ((result or {}).get("operation") or {}).get("id") if isinstance(result, dict) else None}
    # As the app's route does before a review: an older VM's read-only ISO
    # would stop KubeVirt starting it.
    ISOS.unlock(body["ns"])
    preview = VM_REVIEW.preview_vm_power(body)
    plan = preview["capacity"]
    if plan.get("blocked"):
        raise API_V1.ApiError(409, "it cannot start: " + "; ".join(plan.get("blockers") or ["not enough room"]),
                              blockers=list(plan.get("blockers") or []))
    if (plan.get("vm") or {}).get("state_initialization"):
        raise API_V1.ApiError(409, "starting it needs its TPM or EFI state set up first; do that in the app")
    result = VM_REVIEW.reviewed_vm_power({**body, "capacity_token": preview["capacity_token"], "confirm_capacity": True})
    _cache.pop("vms", None)
    job = (result.get("operation") or {}).get("id") if isinstance(result, dict) else None
    return {"warnings": list(plan.get("warnings") or []), "job": job}


def api_scale(ns, name, n):
    """Start or stop a container for /api/v1, under the app's own checks."""
    try:
        result = scale_workload(ns, name, n, confirm_capacity=True, confirm_self=False)
    except WORKLOADS.WorkloadRefused as refused:
        raise API_V1.ApiError(409, str(refused), blockers=list((refused.plan or {}).get("blockers") or [])) from None
    return {"warnings": list((result.get("plan") or {}).get("warnings") or []), "job": None}


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
        users = STORAGE._references_for(STORAGE.claim_references(), ns, name)
    except Exception:
        users = ["something"]
    if users:
        raise ValueError(f"a volume named {name} already exists in {ns} and {', '.join(users)} uses it; "
                         "give this app's volume another name, or choose the existing one to share it")
    return name


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
import homestead_nodes as NODES
import homestead_power_jobs as POWER_JOBS
import homestead_updates as UPDATES
import homestead_vmusage as VMUSAGE
import homestead_cancel as CANCEL
import homestead_joblogs as JOBLOGS
import homestead_console as CONSOLE
import homestead_files as FILES
import homestead_snapshot_files as SNAPSHOT_FILES
import homestead_icons as ICONS
ICONS.bind(DATA_DIR)
import homestead_logos as LOGOS
import homestead_volumes as VOLUMES
import homestead_smart as SMART
import homestead_shares as SHARES
import homestead_nfs as NFS
import homestead_networking as NETWORK
import homestead_flow as FLOW
import homestead_firewall as FIREWALL
import homestead_routes as MODULE_ROUTES
MODULE_ROUTES.bind(lambda key, seconds, fn: cached(key, seconds, fn), _cache)
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
import homestead_deploy as DEPLOY
import homestead_workloads as WORKLOADS
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
import homestead_vm_review as VM_REVIEW
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
PROBE.bind(kget, ksend, DEFAULT_NS, AUTH.smart_signing_key, version=HOMESTEAD_VERSION)
def allocation_probe_capacity(obj, template):
    listing = kget("/api/v1/pods")
    if not isinstance(listing.get("items"), list) or (listing.get("metadata") or {}).get("continue"):
        raise ValueError("Complete workload inventory is unavailable; nothing was changed")
    return ALLOCATION_CAPACITY.plan(obj, template, PLACE.get_nodes(), listing["items"],
                                    get_app_settings()["thresholds"]["memory"]["critical"])


ALLOCATION_PROBE.bind(kget, ksend, DEFAULT_NS, allocation_probe_capacity, version=HOMESTEAD_VERSION,
                      current_hosts=lambda: PLACE.get_nodes(), inspect=ALLOCATION_EVIDENCE.inspect)
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
NODES.bind(kget, ksend, DEFAULT_NS, SYS_NS, get_app_settings, DEFAULT_APP_SETTINGS,
           lambda: WORKLOADS.get_workloads(), STORAGE.get_volumes, parse_cpu, parse_mem, _dns_name)
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
IMP.bind(kget, ksend, create_pvc, DEPLOY.build_deployment, DEFAULT_NS, _cache, HW.features)
IMP.SCAN_DIR = DATA_DIR       # each node's full image list, kept for every replica
AUTH.bind(kget, _auth_ksend, DEFAULT_NS)
CAPACITY_REVIEW.bind(AUTH.review_signing_key)
LH.bind(kget, ksend, _cache, STORAGE_CLASS, DEFAULT_NS)
PLACE.bind(kget, ksend, lambda: cached("nodes", 5, NODES.get_nodes), _cache, HW.features)
POWER.bind(kget, PLACE.impact, LC.quorum_report, lambda: LC.NODE_POWER_ENABLED)
HOLD.bind(kget, ksend, (SELF.NS, NAMES.BRAND), POD_NAME)
# The helper image is read from Homestead's own pod, defined further down.
POWER_JOBS.bind(kget, ksend, lambda read, ns: _self_data_helper_image(read, ns), homestead_running_on, POD_NAME)
REBALANCE.bind(kget, ksend, lambda: LEADER.is_leader())


def rebalance_move(ns, name, node):
    """One container of a rebalance: the manual move's capacity check and
    placement - a preference, not a pin - or a reason to leave it."""
    body = {"ns": ns, "name": name, "node": node, "pin": False}
    proposed, capacity, _ = WORKLOADS.move_capacity_plan(body)
    if capacity.get("blocked"):
        raise ValueError("; ".join(capacity.get("blockers") or []) or f"{node} cannot take it now")
    meta = proposed["metadata"]
    ksend("PUT", f"/apis/apps/v1/namespaces/{meta['namespace']}/deployments/{meta['name']}", proposed)
    PLACE._bust("wl", "ov", "flow", "nodes", "impact:")


CREBALANCE.bind(kget, PLACE.requirements, PLACE.satisfies, rebalance_move, (SELF.NS, NAMES.BRAND), lambda: LEADER.is_leader(),
                PLACE.get_nodes, HOSTACCESS.require_target_as)
POWER.WORKER_GONE, POWER.RESUME = POWER_JOBS.power_worker_gone, POWER_JOBS.resume_power_job
POWER.RESTORE, POWER.UNCORDON = POWER_JOBS.restore_held, POWER_JOBS.allow_scheduling
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
    for cleanup in (STORAGE_CLASSES.cleanup_restore_classes, LH.cleanup_restore_snapshots):
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
PORTAL.bind(kget, ksend, DEFAULT_NS, lambda: cached("wl", 5, WORKLOADS.get_workloads),
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


def _bind_recovery():
    """The job tray's recovery reviews read through server.py's reader, so a
    test that patches it is the one they use."""
    OPS.bind_recovery(lambda *a, **k: kget(*a, **k), lambda *a: storage_helper_admission(*a),
                      lambda *a: storage_restart_admission(*a), lambda: storage_runtime_check())


def storage_runtime_check():
    try:
        return STORAGE_RUNTIME.require(OPS, kget, SELF.NS, SELF.POD, NAMES.BRAND, HOMESTEAD_VERSION, DATA_DIR)
    except STORAGE_WORKFLOW.JOURNAL.Held:
        raise
    except Exception:
        raise STORAGE_WORKFLOW.JOURNAL.Held("Homestead replica compatibility could not be verified; restore cluster and shared-data access before moving storage") from None


_bind_recovery()


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
import homestead_ports as PORTS
import homestead_uptime as UPTIME
import homestead_housekeeping as HOUSEKEEPING
import homestead_schedules as SCHEDULES
import homestead_outage as OUTAGE


import homestead_changes as CHANGES
import homestead_logsearch as LOGSEARCH
import homestead_forecast as FORECAST
import homestead_autoupdate as AUTOUPDATE
import homestead_restore_test as RESTORE_TEST
import homestead_uplinks as UPLINKS
import homestead_root_guard as ROOT_GUARD
import homestead_os_rollout as OS_ROLLOUT
import homestead_passthrough as PASSTHROUGH
import homestead_self_address as SELF_ADDRESS
import homestead_self_health as SELF_HEALTH
import homestead_host_bridge as HOST_BRIDGE
import homestead_host_bonds as HOST_BONDS
import homestead_manifests as MANIFESTS
import homestead_disk_setup as DISK_SETUP
import homestead_disk_v2 as DISK_V2
import homestead_hvimage as HVIMAGE
import homestead_revert as REVERT
OPS.RESOLVERS["reclass"] = storage_move_progress
OPS.RESUMABLE["reclass"] = RECLASS.resumable
OPS.RESOLVERS["protect-run"] = LH.run_status
MOVE_SOURCE.bind(kget, ksend, LH, DEFAULT_NS)
MOVE_ENGINE.bind(kget, ksend, LH, MOVE, NETWORK, OPS, DATA_DIR, DEFAULT_NS, guard=lambda ns, name: guard_managed_smb(ns, name))


def _late(*names):
    """server.py's helpers by name, each looked up when called, so a module's
    routes reach the same function server.py's own code does - and a test
    that replaces one here replaces it there."""
    return {name: (lambda name: lambda *args, **kwargs: globals()[name](*args, **kwargs))(name) for name in names}


WORKLOADS.bind(_late(*WORKLOADS.NEEDS), api=API, token=TOKEN, ssl_context=CTX, default_ns=DEFAULT_NS,
               data_dir=DATA_DIR, system_namespaces=SYS_NS, platform_namespaces=PLATFORM_NS)
DEPLOY.bind(_late(*DEPLOY.NEEDS), default_ns=DEFAULT_NS, version=HOMESTEAD_VERSION)
UPDATES.bind_routes(_late(*UPDATES.ROUTE_NEEDS))
IMP.bind_routes({**_late("guard_managed_smb", "preview_import", "reviewed_import"),
                 "workload_service_names": lambda ns, name: NETWORK.workload_service_names(ns, name)})
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
ADDONS.bind(kget, ksend, PLATFORM.detect, NODES.node_temps)
MACVTAP.bind(kget, ksend, ADDONS, PLATFORM.detect)
BASELINE.bind(kget, ADDONS, PLATFORM.detect, DEFAULT_NS, DATA_DIR, MACVTAP, PROBE, HOMESTEAD_VERSION)
COMPONENTS.bind(kget, ksend, PLATFORM.detect, lambda cfg: HELM.upgrade(cfg), ADDONS, _ktext=lambda path: raw_get(path))
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
VMS.bind(kget, ksend, RESOURCES.events_for, lambda annotations: display_icon(annotations), DEFAULT_NS,
         lambda b: set_vm_logos(b), lambda b: set_vm_monitoring(b))
# Looked up as each call is made, so what a test puts in their place is used.
VM_REVIEW.bind(lambda *a, **k: kget(*a, **k), lambda *a, **k: ksend(*a, **k), DEFAULT_NS,
               lambda *a, **k: _dns_name(*a, **k), lambda current: rollout_review_context(current),
               lambda: get_app_settings(), lambda *a: vm_default_class(*a), lambda ip: vm_address_problem(ip),
               lambda: vm_create_options())
VMUSAGE.bind(kget)
VMS.platform, VMS.images = PLATFORM.detect, IMP.list_vm_images
STORAGE_CLASSES.bind(kget, ksend, storage_classes, _dns_name, lambda: NODES.node_temps(), _cache, DATA_DIR, DEFAULT_NS,
                     selectable_storage_classes, shared_storage_classes, storage_class_facts)
STORAGE.bind(kget, ksend, storage_classes, create_pvc, age_secs, _cache, SYS_NS, DEFAULT_NS, storage_runtime_check)
LHCAP.bind(kget, ksend, STORAGE_CLASSES.v2_engine_status)
LHREBUILD.bind(kget, ksend, lambda: (get_app_settings().get("longhorn") or {}).get("offline_rebuilding", True),
               settings=get_app_settings, save_settings=save_app_settings)
import homestead_lhv2_setup as LHV2_SETUP
LHV2_SETUP.bind(kget, ksend, STORAGE_CLASSES.v2_engine_status, OPS, DEFAULT_NS)
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
NODE_PARITY.bind(kget, ksend, HOSTRUN, PLATFORM.detect, NODES.node_temps, DATA_DIR, (SELF.NS, NAMES.BRAND))
HOST_OS.bind(kget, HOSTRUN, PLATFORM.detect, DATA_DIR)
PORTS.bind(DATA_DIR)
UPTIME.bind(DATA_DIR, setting=lambda b: set_uptime_setting(b))
HOUSEKEEPING.bind(DATA_DIR, kget=lambda path: kget(path), portal_links=lambda: PORTAL.stored())
SCHEDULES.bind(DATA_DIR, items=lambda: scheduled_items(), setter=lambda b: set_schedule(b))
OUTAGE.bind(DATA_DIR, kget=lambda path: kget(path), ksend=lambda *a, **k: ksend(*a, **k),
            dns_name=lambda value, label: _dns_name(value, label), guard=lambda ns, name: guard_managed_smb(ns, name))
CHANGES.bind(DATA_DIR, plan=lambda b: change_undo_plan(b), ksend=lambda *a, **k: ksend(*a, **k))
FORECAST.bind(DATA_DIR)


OPS.RESOLVERS["host-os"] = HOST_OS.status
ROOT_GUARD.bind(kget, ksend, PLATFORM.detect, NODES.node_temps, DATA_DIR)
PASSTHROUGH.bind(kget, ksend, HOSTRUN, PLATFORM.detect, DATA_DIR)
SELF_ADDRESS.bind(kget, NETWORK, OBJECTS, SELF.NS, os.environ.get("PORT", "8080"), SMB_NAMESPACE, SMB_NAME,
                  follow_fleet=lambda: _follow_fleet_address())


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
OS_ROLLOUT.bind(kget, PLATFORM.detect, HOST_OS, POWER_JOBS.rollout_reboot, operation_item, LC.set_cordon, own_node, DATA_DIR,
                lambda rollout: OPS.start("os-rollout", f"Update every host's OS ({len(rollout['nodes'])} hosts)",
                                          {"kind": "Node", "name": ", ".join(rollout["nodes"])[:200]}, "/nodes",
                                          {"rollout": rollout["id"]},
                                          "In the weekly window" if rollout["reason"] == "schedule" else "Starting"),
                lambda node, since: POWER_JOBS.rollout_power_job(node, since))
OPS.RESOLVERS["os-rollout"] = OS_ROLLOUT.status
DISK_SETUP.bind(HOSTRUN)
HOST_BRIDGE.bind(HOSTRUN, kget, ksend)
OPS.RESOLVERS["host-bridge"] = HOST_BRIDGE.status
HOST_BONDS.bind(HOSTRUN, kget, ksend, PLATFORM.detect)
OPS.RESOLVERS[HOST_BONDS.KIND] = HOST_BONDS.status


UPLINKS.bind(kget, ksend, lambda: NODES.node_temps())
OPS.RESOLVERS[UPLINKS.KIND] = UPLINKS.status
OPS.RESOLVERS[AUTOUPDATE.KIND] = lambda item: AUTOUPDATE.resolve(item, OPS.checkpoint)
OPS.RESOLVERS[RESTORE_TEST.KIND] = lambda item: RESTORE_TEST.resolve(item, OPS.checkpoint)
RESTORE_TEST.bind(kget, ksend, NAMES, lambda volume: LH.backups(volume), LH.restore_backup, OPS._volume_restore,
                  LH.cleanup_restore_snapshots, LHCAP.status, lambda target: UPTIME.ask_ports(target), lambda: SELF.NS,
                  lambda: cached("wl", 5, WORKLOADS.get_workloads), get_app_settings, save_app_settings, _dns_name)


def _storage_classes():
    return {c["metadata"]["name"]: c.get("parameters") or {}
            for c in kget("/apis/storage.k8s.io/v1/storageclasses").get("items", [])}


def restore_tests_tick():
    """On the leader, hourly: test the app longest untested, when switched on."""
    settings = get_app_settings()
    chosen = RESTORE_TEST.pick(cached("wl", 5, WORKLOADS.get_workloads), OPS.list_operations(),
                               settings.get("restore_tests", {}).get("enabled", False),
                               update_policy_status(settings)["window_open"], time.time(), _storage_classes())
    if chosen:
        RESTORE_TEST.start_test(chosen["ns"], chosen["name"], "monthly")
    return chosen


def _restore_tests_loop():
    while True:
        if LEADER.is_leader():
            try:
                restore_tests_tick()
                beat("restore-tests", 3600, leader_only=True)
            except Exception as error:
                beat("restore-tests", 3600, error, leader_only=True)
                print(f"restore-tests: {str(error)[:160]}", flush=True)
        time.sleep(3600)


def _prune_snapshot(volume, name):
    """Remove one old before-update snapshot, as Data Protection would."""
    plan = SNAPSHOT_DELETE.plan(volume, name)
    if not plan["blockers"] and not plan["deleting"] and not plan["removed"]:
        SNAPSHOT_DELETE.start({**plan, "confirmation": name}, OPS)


AUTOUPDATE.bind(kget, ksend, UPDATES, lambda: UPTIME.report(),
                lambda volume, name, annotations: LH.create_snapshot(volume, name, annotations),
                lambda volume: LH.snapshots(volume), _prune_snapshot, NAMES)


def autoupdate_tick():
    """On the leader: start the next automatic update if the window is open,
    and tidy old snapshots after ones that worked. Returns the app started."""
    status = update_policy_status()
    operations = OPS.list_operations()
    AUTOUPDATE.tidy(operations)
    report = UPDATES._LATEST.get("report") or {}
    chosen = AUTOUPDATE.pick(cached("wl", 5, WORKLOADS.get_workloads), report, UPTIME.report(), operations,
                             status["window_open"], status["policy"])
    if not chosen:
        return None
    OPS.start(AUTOUPDATE.KIND, f"Update {chosen['name']} automatically",
              {"kind": "Deployment", "name": chosen["name"], "namespace": chosen["ns"]},
              "/containers?" + urllib.parse.urlencode({"find": chosen["name"]}),
              {"namespace": chosen["ns"], "name": chosen["name"], "phase": "snapshot"},
              "Snapshotting its volumes before the update")
    _cache.pop("wl", None)
    return chosen


def _autoupdate_loop():
    while True:
        if LEADER.is_leader():
            try:
                autoupdate_tick()
                beat("auto-updates", 60, leader_only=True)
            except Exception as error:
                beat("auto-updates", 60, error, leader_only=True)
                print(f"auto-updates: {str(error)[:160]}", flush=True)
        time.sleep(60)


def set_update_mode(b):
    """Whether an app updates itself inside the maintenance window. An annotation, so nothing restarts."""
    ns, name = _dns_name(b.get("ns"), "namespace"), _dns_name(b.get("name"), "workload name")
    guard_managed_smb(ns, name)
    chosen = str(b.get("mode") or "")
    if chosen not in ("auto", "manual"):
        raise ValueError("choose auto or manual")
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    if chosen == "auto" and UPDATES.managed_by(dep):
        raise ValueError(UPDATES.managed_message(ns, name, UPDATES.managed_by(dep)))
    if chosen == "auto" and is_self(ns, name):
        raise ValueError("Homestead updates itself from Settings › Updates")
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
          {"metadata": {"annotations": {NAMES.key(AUTOUPDATE.MODE): "auto" if chosen == "auto" else None}}},
          ctype="application/merge-patch+json")
    _cache.pop("wl", None)
    return {"ok": True, "mode": chosen,
            "detail": f"{name} updates itself in the maintenance window" if chosen == "auto" else f"{name} waits for you to update it"}
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
DISKS.bind(kget, ksend, NODES.node_temps)
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
CANCEL.bind(kget, ksend, delete_workload, lambda: STORAGE_CLASSES.cleanup_restore_classes())
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
    take("ports", lambda: PORTS.alert_facts(cached("ports", 20, NODES.ports_report)))
    take("uptime", lambda: UPTIME.alert_facts(UPTIME.report()))
    take("schedules", lambda: SCHEDULES.alert_facts())
    take("outage", lambda: OUTAGE.alert_facts())
    take("housekeeping", lambda: HOUSEKEEPING.alert_facts(cached("housekeeping", 600, HOUSEKEEPING.report)))
    take("forecast", lambda: FORECAST.alert_facts(FORECAST.report()))
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


VIP_QUICK = 5          # a look for a missing address
VIP_FULL = 30          # a full pass regardless


def _vip_loop():
    """kube-vip can answer for an address and the Service lose it from its
    status - k3s's ServiceLB empties it when the pods change - and then every
    port there is refused. The leader records it again, as kube-vip would
    have (homestead_vips.py): within seconds of it going missing (#365), and
    in a full pass every VIP_FULL seconds."""
    last_full = 0.0
    while True:
        if LEADER.is_leader():
            try:
                platform = PLATFORM.detect()
                now = time.time()
                if now - last_full >= VIP_FULL or VIPS.missing(platform):
                    last_full = now
                    if VIPS.keep(platform):
                        with _lock:
                            _cache.pop("network", None)
                            _cache.pop("flow2", None)
                beat("vips", VIP_FULL, leader_only=True)
            except Exception as error:
                beat("vips", VIP_FULL, error, leader_only=True)
                print(f"VIPs: {str(error)[:160]}", flush=True)
        time.sleep(VIP_QUICK)


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
                            BASELINE.operation(row, "Install")
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
        time.sleep(15)


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
                        repaired = STORAGE_CLASSES.repair_restore_classes()
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
                    fixed = STORAGE_CLASSES.reconcile_default_class()
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


def fit_own_strategy():
    """An update from this page changes only the image, so a Deployment that
    was once set to roll keeps rolling. Put right at start-up what the data
    volume can take. The strategy is not part of the pod template, so this
    starts no rollout."""
    try:
        ns, name = SELF.NS, NAMES.BRAND
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        want = SELF_HEALTH.own_strategy(homestead_data_volume(dep)["shareable"])
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


CLUSTER_SHUTDOWN.bind(cluster_shutdown)


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
        _, public, binding, _ = reviewer(read)._snapshot(approved["config"])
        return SELF_DATA_REVIEW.recheck_binding(approved["binding"], binding, public.get("stages"))
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


# Looked up when asked, so a test that patches one is the one used.
SELF_HEALTH.bind(lambda *a, **k: kget(*a, **k), lambda *a, **k: ksend(*a, **k),
                 lambda dep=None: homestead_data_volume(dep), _heartbeats,
                 lambda: NODES.node_temps(), lambda: SHARE_SERVERS.samba_state(), DEFAULT_NS, LB_IP, HOMESTEAD_VERSION)
# Join plans are gone; a job one left in Activity says so rather than erroring.
OPS.RESOLVERS["onboard"] = lambda item: ("cancelled", item.get("progress", 0),
                                         "Join plans were replaced by the install guide")
# A cleanup is recorded once it has happened, so it reads as done straight away.
OPS.RESOLVERS["cluster-cleanup"] = lambda item: ("succeeded", 100, item.get("message", ""))
VOLUMES.bind(kget, ksend, LH.snapshots, LH.backups, _cache, SYS_NS, DEFAULT_NS)
SHARES.bind(kget, ksend, create_pvc, SMB_NAMESPACE, _cache)
SHARES.install = SHARE_SERVERS.install_samba
SHARE_SERVERS.bind(kget, ksend, lambda cfg: DEPLOY.build_deployment(cfg), SMB_NAMESPACE, _cache,
                   shared_storage_classes, lambda: STORAGE_CLASS, share_storage_options)
NETWORK.bind(kget, ksend, SYS_NS, DEFAULT_NS, LB_IP, lambda ns, name: guard_managed_smb(ns, name), lambda: NODES.node_temps())
FLOW.bind(lambda path: kget(path), SYS_NS, lambda annotations: display_icon(annotations), STORAGE._moving_copy,
          parse_cpu, parse_mem, age_secs)
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
# Looked up when asked, so a test that patches one is the one the guide reads.
SETUP.bind(DATA_DIR, overview=lambda: get_overview(), nodes=lambda: NODES.get_nodes(), workloads=lambda: WORKLOADS.get_workloads(),
           vm_networks=lambda: vm_network_details(strict=True), storage_classes=lambda: storage_classes(),
           samba=lambda: SHARE_SERVERS.samba_state())
API_V1.bind(nodes=lambda: cached("nodes", 5, NODES.get_nodes), workloads=lambda: cached("wl", 5, WORKLOADS.get_workloads),
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
CLUSTER.bind(kget, SYS_NS, lambda: cached("nodes", 5, NODES.get_nodes), lambda: get_overview())
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


# Browser routes serve the same authenticated application shell. Keep this an
# explicit allowlist: an unknown path must not accidentally shadow an API 404.
SPA_ROUTES = frozenset({
    "/", "/architecture", "/nodes", "/deploy", "/containers", "/vms",
    "/app-store", "/shares", "/volumes", "/image-cache", "/data-protection", "/portal", "/helm", "/resources",
    "/schedules", "/monitoring", "/import", "/vms/import", "/events", "/networking", "/system/cluster", "/settings", "/setup",
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
# Declared beside their handlers (homestead_routes.py) and admin-only.
ADMIN_ROUTES |= {route.path for route in MODULE_ROUTES.table().values() if route.role == "admin"}
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
    "workloads": lambda: cached("wl", 5, WORKLOADS.get_workloads),
    "vms": lambda: cached("vms", 5, VMS.list_vms),
    "nodes": lambda: cached("nodes", 5, NODES.get_nodes),
    "volumes": lambda: cached("vol", 8, STORAGE.get_volumes),
    "flow": lambda: FLOW.view(),
}


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
        # Which Homestead answered: a page left open across an update of
        # Homestead itself sees it and offers to reload (auth.js).
        self.send_header("X-Homestead-Version", HOMESTEAD_VERSION)
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
            if ctype == "image/svg+xml":
                # Opened on its own, an SVG is a document: nothing in it may run.
                self.send_header("Content-Security-Policy", ICONS.SVG_POLICY)
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

    def _module_route(self, method, path, query, body):
        """A route a feature module declares (homestead_routes.py), answered;
        False when server.py answers it itself. The guard has already
        checked the role the module declared for it."""
        route = MODULE_ROUTES.find(method, path)
        if route is None:
            return False
        answer = route.handler(MODULE_ROUTES.Request(method, path, query, body,
                                                     getattr(self, "user", None), getattr(self, "role", None)))
        if isinstance(answer, MODULE_ROUTES.Stream):
            return self._send_stream(answer) or True
        if isinstance(answer, MODULE_ROUTES.Raw):
            self._extra_headers += [("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")]
            self._send(200, answer.body, answer.ctype)
        elif isinstance(answer, MODULE_ROUTES.Reply):
            self._send(answer.code, answer.body, answer.ctype)
        else:
            self._send(200, answer)
        return True

    def _send_stream(self, stream):
        """A download as it is read. Without a size the connection ends it; a
        failure part-way closes it, so a cut-off file is never taken as whole."""
        self.send_response(200)
        self.send_header("Content-Type", stream.ctype)
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(stream.filename, safe=""))
        self.send_header("Cache-Control", "no-store")
        if stream.size is not None:
            self.send_header("Content-Length", str(stream.size))
        else:
            self.close_connection = True
            self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        try:
            for chunk in stream.chunks:
                self.wfile.write(chunk)
        except Exception:
            self.close_connection = True

    def _begin(self):
        """Per request: a connection can carry several, and the handler stays."""
        CHANGES.REQUEST.user = None
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
            if self._module_route("GET", p, q, None):
                return
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
                                        # Where Deploy puts a new app unless told otherwise.
                                        "default_namespace": DEFAULT_NS if who else None,
                                        "roles": list(AUTH.ROLES)})
            if p == "/api/fleet":
                self._who()
                return self._send(200, FLEET.summary(via=self._fleet_from))
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
                what = p.rsplit("/", 1)[-1]
                rows, missing = FLEET.gather(what, FLEET_LISTS[what], self.user, self.role)
                if missing:
                    self._extra_headers.append(("X-Homestead-Fleet-Missing",
                                                urllib.parse.quote(json.dumps(missing))))
                return self._send(200, rows)
            if p == "/api/auth/preferences/dashboard":
                return self._send(200, AUTH.dashboard_preferences(self.user))
            if p == "/api/portal/status":
                return self._send(200, portal_status(force=(q.get("force") or [""])[0] == "1"))
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
            if p in ("/api/resources/list", "/api/resources/object", "/api/resources/reveal"):
                arg = lambda key: (q.get(key) or [""])[0]
                if p == "/api/resources/list":
                    return self._send(200, RESOURCES.list_objects(arg("group"), arg("version"), arg("resource"), arg("ns")))
                return self._send(200, RESOURCES.get_object(arg("group"), arg("version"), arg("resource"), arg("ns"),
                                                            arg("name"), reveal=p.endswith("reveal")))
            if p == "/api/mqtt/preview":
                snap = mqtt_snapshot()
                return self._send(200, {"states": [{"topic": t, "payload": v} for t, v in MQTT.states(MQTT.load(), snap)]})
            # What this cluster offers another one. Read-only.
            if p == "/api/onboard/guide":
                return self._move(ONBOARD.guide)
            if p == "/api/cluster/cleanup":
                return self._move(ONBOARD.cleanup_report)
            if p == "/api/cluster/removal":
                return self._move(lambda: ONBOARD.removal_plan((q.get("node") or [""])[0]))
            if p == "/api/snapshot-files/plan":
                return self._send(200, SNAPSHOT_FILES.review((q.get("volume") or [""])[0], (q.get("snapshot") or [""])[0]))
            if p in ("/api/snapshot-files/status", "/api/snapshot-files/list", "/api/snapshot-files/download"):
                ns, session = (q.get("namespace") or [""])[0], (q.get("session") or [""])[0]
                if p.endswith("/status"):
                    return self._send(200, SNAPSHOT_FILES.status(ns, session))
                path = (q.get("path") or [""])[0]
                if p.endswith("/download"):
                    return self._send_stream(SNAPSHOT_FILES.download(ns, session, path))
                return self._send(200, SNAPSHOT_FILES.read(ns, session, "list", path))
            if p == "/api/logos":
                try:
                    return self._send(200, logo_choices((q.get("ns") or [""])[0], (q.get("name") or [""])[0],
                                                        (q.get("q") or [None])[0], (q.get("kind") or ["workload"])[0]))
                except Exception as e:
                    return self._send(502, {"error": f"app feed unavailable: {e}"})
            if p == "/api/logos/missing":
                try:
                    apps = DEPLOY.fetch_appstore()
                except Exception as e:
                    return self._send(502, {"error": f"app feed unavailable: {e}"})
                return self._send(200, {"apps": LOGOS.missing(cached("wl", 5, WORKLOADS.get_workloads), apps, helm_chart_logos())})
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
            # Whose request this is, for the change history (homestead_changes).
            CHANGES.REQUEST.user = getattr(self, "user", None)
            if int(self.headers.get("Content-Length") or 0) > MAX_BODY:
                return self._send(413, {"error": "that request is larger than Homestead accepts"})
            b = self._body()
            if not isinstance(b, dict):
                return self._send(400, {"error": "a JSON object is required"})
            if p in ("/api/move", "/api/move/preview", "/api/image-updates/apply", "/api/image-updates/rollback", "/api/image-updates/preview"):
                require_workload_target(b.get("ns") or DEFAULT_NS, b.get("name") or "")
            addr = self._client_ip()
            if p.startswith("/api/v1/"):
                return self._api_v1("POST", p, urllib.parse.parse_qs(u.query), b)
            if self._module_route("POST", p, Query(urllib.parse.parse_qs(u.query)), b):
                return
            if p == "/api/auth/keys":
                # Made in this Homestead's own app, by someone signed in to it:
                # never relayed from a linked cluster.
                if self._fleet_from:
                    return self._send(403, {"error": "make API keys in this Homestead's own app"})
                made = API_KEYS.create(b.get("name"), b.get("scopes"), b.get("ttl_seconds"), b.get("networks"), self.user)
                self._signin("key-added", self.user, detail=f"{made['key']['name']}: {', '.join(made['key']['scopes'])}")
                return self._send(200, {"ok": True, **made})
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
            if p == "/api/namespaces/create":
                return self._move(lambda: NSMOD.create(b.get("name")))
            if p == "/api/namespaces/delete":
                return self._move(lambda: NSMOD.delete(b.get("name"), str(b.get("confirm") or "")))
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
            if p == "/api/snapshot-files/start":
                return self._send(200, storage_volume_action(b.get("volume"), lambda: SNAPSHOT_FILES.start(b)))
            if p == "/api/snapshot-files/close":
                return self._send(200, SNAPSHOT_FILES.close(b.get("namespace"), b.get("session")))
            if p == "/api/cluster/remove-node":
                return self._move(lambda: ONBOARD.remove_node(b.get("node"), bool(b.get("accept_loss")),
                                                              bool(b.get("gone"))))
            if p == "/api/cluster/cleanup/run":
                return self._move(lambda: ONBOARD.cleanup(b.get("kind"), b.get("name") or "", bool(b.get("force"))))
            if p == "/api/cluster/shutdown":
                return self._send(202, cluster_shutdown(review=True).start(b, OPS))
            if p == "/api/self/samba":
                return self._send(200, SHARE_SERVERS.set_samba(bool(b.get("enabled")), str(b.get("address") or "").strip()))
            if p == "/api/self/nfs":
                return self._send(200, SHARE_SERVERS.set_nfs(bool(b.get("enabled")), str(b.get("address") or "").strip()))
            if p == "/api/addons/smb/remove":
                if b.get("confirm") != SMB_NAME:
                    raise ValueError(f"type {SMB_NAME} to remove the SMB server")
                return self._send(200, SHARE_SERVERS.remove_samba())
            if p == "/api/addons/nfs/remove":
                if b.get("confirm") != NFS.NAME:
                    raise ValueError(f"type {NFS.NAME} to remove the NFS server")
                return self._send(200, SHARE_SERVERS.remove_nfs())
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
                    state = SHARE_SERVERS.samba_state()
                    if state.get("name") == "samba" and state.get("installed"):
                        SHARE_SERVERS.install_samba()
                    result = SHARES.reconcile_samba(SHARE_SERVERS.SAMBA_IMAGE)
                beat("samba", 60, leader_only=True)
                if result.get("state") == "repaired":
                    print("network shares: restored SMB settings from the share inventory", flush=True)
            except Exception as error:
                beat("samba", 60, error, leader_only=True)
                print(f"network shares: {str(error)[:180]}", flush=True)
            try:
                with self_data_activity():
                    nfs_result = SHARE_SERVERS.reconcile_nfs()
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
    threading.Thread(target=_uptime_loop, name="uptime", daemon=True).start()
    threading.Thread(target=_housekeeping_loop, name="housekeeping", daemon=True).start()
    threading.Thread(target=_schedules_loop, name="schedules", daemon=True).start()
    threading.Thread(target=_changes_loop, name="changes", daemon=True).start()
    threading.Thread(target=_forecast_loop, name="forecast", daemon=True).start()
    threading.Thread(target=_unifi_loop, name="unifi", daemon=True).start()
    threading.Thread(target=_autoupdate_loop, name="auto-updates", daemon=True).start()
    threading.Thread(target=_restore_tests_loop, name="restore-tests", daemon=True).start()
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
