#!/usr/bin/env python3
"""
Homestead - a friendly homelab control plane for Harvester, Rancher and Longhorn.
Pure Python stdlib: no pip install at runtime, so it starts even with no internet.
"""
import html, json, os, re, signal, ssl, sys, time, threading, urllib.request, urllib.parse, urllib.error
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
import homestead_allocation_evidence as ALLOCATION_EVIDENCE
import homestead_rename as RENAME
import homestead_copy_job as COPY_JOB
import homestead_import_job as IMPORT_JOB
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

import homestead_cache as CACHE
_cache, _lock = CACHE._cache, CACHE._lock  # the same dict and lock, shared

# Each background task's last beat, shared by the leader (homestead_heartbeats).
from homestead_heartbeats import HEART, beat, heartbeats as _heartbeats


# The Homestead replica a host power job is running in. A drain can evict
# that very replica; the job then says which pod to look for, and the leader
# carries it on from another replica (homestead_power_jobs).
POD_NAME = os.environ.get("HOSTNAME", "")

OWN_GROUP = "Homestead"

VENDOR_TYPES = {".js": "application/javascript", ".css": "text/css", ".ttf": "font/ttf", ".woff2": "font/woff2",
                ".json": "application/json", ".svg": "image/svg+xml", ".map": "application/json",
                ".md": "text/markdown; charset=utf-8"}

# Helpers moved on (track 2) by name: the module each lives in now. A test
# patches them there, so _late looks them up there.
MOVED = {}
MOVED.update(dict.fromkeys(("cached",), CACHE))


def _late(*names):
    """server.py's helpers by name, each looked up when called, so a module's
    routes reach the same function server.py's own code does - and a test
    that replaces one here (or, once moved, in its module) replaces it there."""
    def find(name):
        return getattr(MOVED[name], name) if name in MOVED else globals()[name]
    return {name: (lambda name: lambda *args, **kwargs: find(name)(*args, **kwargs))(name) for name in names}


# Feature code moved out of server.py (track 1). Each module reaches the
# helpers server.py still owns through _late, so a test that patches one on
# server patches it there too; server.py imports back the names it still uses.
import homestead_overview_server as OVERVIEW_ROUTES
from homestead_overview_server import (
    _alerts_loop, _diagnostic_read, _history_loop, _sampler, _schedules_loop, _uptime_loop,
    api_scale, change_undo_plan, is_self, protection_issues, restart_workload, scheduled_items,
    set_schedule, set_uptime_setting, set_vm_monitoring,
)
MOVED.update(dict.fromkeys(("api_scale", "clear_unstarted_pods", "get_overview", "guard_managed_smb", "guard_self", "is_self", "restart_workload", "scale_workload", "workload_start_plan"), OVERVIEW_ROUTES))
OVERVIEW_ROUTES.bind_routes(_late(*OVERVIEW_ROUTES.ROUTE_NEEDS), **{name: globals()[name] for name in OVERVIEW_ROUTES.VALUES})
import homestead_cluster_server as CLUSTER_ROUTES
from homestead_cluster_server import (
    _autoupdate_loop, _baseline_loop, _detached_copies_loop, _hardware_loop, _host_console_loop,
    _housekeeping_loop, _os_updates_loop, _jobs_loop, _upgrade_node_probe,
    allocation_probe_capacity, cluster_shutdown, pod_container_rows, preview_image_update,
    reviewed_image_update, set_update_mode,
)
CLUSTER_ROUTES.bind_routes(_late(*CLUSTER_ROUTES.ROUTE_NEEDS), **{name: globals()[name] for name in CLUSTER_ROUTES.VALUES})
import homestead_network_server as NETWORK_ROUTES
from homestead_network_server import (
    _follow_fleet_address, _unifi_loop, _vip_loop, edit_lan, installer_vip, mqtt_snapshot,
    record_lan, vm_network_details, vm_networks, vm_subnets,
)
NETWORK_ROUTES.bind_routes(_late(*NETWORK_ROUTES.ROUTE_NEEDS), **{name: globals()[name] for name in NETWORK_ROUTES.VALUES})
import homestead_storage_server as STORAGE_ROUTES
from homestead_storage_server import (
    _class_made_for, _files_loop, _host_fix_loop, _internal_class, _longhorn_copies_loop,
    _prune_snapshot, _pvc_rows, _restore_tests_loop, _samba_loop, _snapshot_files_loop,
    _storage_pending_loop, _storage_runtime_loop, class_selectable, cleanup_restores,
    guard_smb_object, homestead_part, is_managed_nfs, is_managed_smb, serves_many,
    share_storage_options, shared_storage_classes, storage_class_facts, storage_runtime_check,
)
MOVED.update(dict.fromkeys(("cleanup_restores", "ensure_claim", "storage_helper_admission", "storage_restart_admission"), STORAGE_ROUTES))
STORAGE_ROUTES.bind_routes(_late(*STORAGE_ROUTES.ROUTE_NEEDS), **{name: globals()[name] for name in STORAGE_ROUTES.VALUES})
import homestead_vm_server as VM_ROUTES
from homestead_vm_server import (
    _remove_cluster_vm, _vm_image_disks, _vmstore_loop, api_vm_power, create_vm_with_address,
    set_vm_logos, vm_create_options, vm_default_class,
)
MOVED.update(dict.fromkeys(("prepare_lan", "vm_address_problem", "vm_default_class"), VM_ROUTES))
VM_ROUTES.bind_routes(_late(*VM_ROUTES.ROUTE_NEEDS), **{name: globals()[name] for name in VM_ROUTES.VALUES})
import homestead_deploy_server as DEPLOY_ROUTES
from homestead_deploy_server import (
    _moves_loop, _pull_progress, _service_listeners, additional_container_configs,
    apply_deploy_bindings, apply_generated_secrets, apply_reviewed_edit, compose_apply,
    compose_preview, compose_report, copy_admission, delete_workload, deploy_capacity_plan,
    deploy_options, deployment_volumes, ensure_profile_compatible, hardware_device_paths,
    operation_item, prepare_deploy_network, preview_import, rebalance_move,
    redact_deployment_preview, reviewed_deploy, reviewed_import, rollout_review_context,
)
MOVED.update(dict.fromkeys(("persist_icon_config",), DEPLOY_ROUTES))
DEPLOY_ROUTES.bind_routes(_late(*DEPLOY_ROUTES.ROUTE_NEEDS), **{name: globals()[name] for name in DEPLOY_ROUTES.VALUES})
import homestead_self_data_server as SELF_DATA_ROUTES
from homestead_self_data_server import (
    _reconcile_permissions, _replace_own_pod, fit_own_strategy, fleet_address, homestead_running_on,
    own_node, self_data_file_scope, self_stop_warning,
)
MOVED.update(dict.fromkeys(("_require_no_data_handoff", "_self_data_helper_image", "homestead_data_volume"), SELF_DATA_ROUTES))
SELF_DATA_ROUTES.bind_routes(_late(*SELF_DATA_ROUTES.ROUTE_NEEDS), **{name: globals()[name] for name in SELF_DATA_ROUTES.VALUES})
import homestead_kube as KUBE_HELPERS
from homestead_kube import (
    _auth_ksend, _guarded_ksend, _never_started, age_secs, api_path, kget, ksend, ktable,
    logsearch_read, parse_cpu, parse_mem, raw_get,
)
MOVED.update(dict.fromkeys(("_ksend", "kget", "ksend", "require_workload_target"), KUBE_HELPERS))
KUBE_HELPERS.bind_routes(_late(*KUBE_HELPERS.ROUTE_NEEDS), **{name: globals()[name] for name in KUBE_HELPERS.VALUES})
import homestead_names_helpers as NAMES_HELPERS
from homestead_names_helpers import (
    _dns_name, display_icon, own_group,
)
NAMES_HELPERS.bind_routes(_late(*NAMES_HELPERS.ROUTE_NEEDS), **{name: globals()[name] for name in NAMES_HELPERS.VALUES})
import homestead_settings_helpers as SETTINGS_HELPERS
from homestead_settings_helpers import (
    _config_restored, get_app_settings, save_app_settings,
)
MOVED.update(dict.fromkeys(("_fleet_rename", "enforce_update_policy", "get_app_settings", "update_policy_status"), SETTINGS_HELPERS))
SETTINGS_HELPERS.bind_routes(_late(*SETTINGS_HELPERS.ROUTE_NEEDS), **{name: globals()[name] for name in SETTINGS_HELPERS.VALUES})
import homestead_web_paths as WEB_PATHS
from homestead_web_paths import (
    is_app_identity, is_asset_path, is_icon_png, is_page_path, is_public_path, is_spa_route,
    is_vendor_path,
)
WEB_PATHS.bind_routes(_late(*WEB_PATHS.ROUTE_NEEDS), **{name: globals()[name] for name in WEB_PATHS.VALUES})


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
    _self_data_fence = SELF_DATA_FENCE.Fence(KUBE_HELPERS.kget, namespace, NAMES.BRAND,
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
        OPS.stored()
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


def _settings_map():
    return NAMES.object_name("settings", DEFAULT_NS)


def app_settings_payload():
    settings = json.loads(json.dumps(CACHE.cached(SETTINGS_HELPERS.REVISION_KEY, 15, SETTINGS_HELPERS.app_settings_with_revision)))
    kube = SETTINGS_HELPERS.kube_version(KUBE_HELPERS.kget)
    settings["info"] = {"version": HOMESTEAD_VERSION, "namespace": DEFAULT_NS,
                        "storage_class": STORAGE_CLASS, "vip": LB_IP,
                        "kubernetes": kube, "node_probe": PROBE.status(),
                        "permissions": SELF.status()}
    return settings


# Looked up when asked, so a test that patches either is the one the route calls.
APP_SETTINGS.bind(lambda: app_settings_payload(), lambda value: save_app_settings(value))


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


import homestead_share_servers as SHARE_SERVERS
SMB_NAME = SHARE_SERVERS.SMB_NAME


def storage_classes():
    """Every StorageClass, with the one fact that decides if RWX will work.

    A class with migratable=true hands out two-controller volumes so a VM disk
    can live-migrate. Longhorn's CSI driver refuses to filesystem-mount those,
    so a ReadWriteMany claim created on such a class can never be mounted by a
    pod - it binds happily and then strands whatever tries to use it.
    """
    rows = []
    for item in KUBE_HELPERS.kget("/apis/storage.k8s.io/v1/storageclasses").get("items", []):
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


def selectable_storage_classes(rows=None):
    """Classes a person may pick for their own workloads - the default first."""
    rows = [row for row in (rows if rows is not None else storage_classes()) if class_selectable(row)]
    return [row["name"] for row in sorted(rows, key=lambda row: (row["name"] != STORAGE_CLASS, row["name"]))]


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
    return (send or KUBE_HELPERS.ksend)("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", body)


# expose a tiny shim module so helper modules can reach raw_get without a cycle
import types as _types
_shim = _types.ModuleType("homestead_shim")
_shim.raw_get = raw_get
sys.modules["homestead_shim"] = _shim

import homestead_lifecycle as LC, homestead_node_actions as NODE_ACTIONS
import homestead_stale_mount as STALE_MOUNT
import homestead_imports as IMP, homestead_vm_create as VM_CREATE
import homestead_auth as AUTH
import homestead_longhorn as LH, homestead_longhorn_backups as LH_BACKUPS, homestead_longhorn_volumes as LH_VOLUMES, homestead_longhorn_restore as LH_RESTORE
import homestead_place as PLACE
import homestead_hardware as HW
import homestead_nodes as NODES
import homestead_power_jobs as POWER_JOBS
import homestead_updates as UPDATES, homestead_update_rollouts as UPDATE_ROLLOUTS, homestead_update_registry as UPDATE_REGISTRY
import homestead_vmusage as VMUSAGE
import homestead_cancel as CANCEL
import homestead_joblogs as JOBLOGS
import homestead_console as CONSOLE
import homestead_files as FILES
import homestead_snapshot_files as SNAPSHOT_FILES
import homestead_icons as ICONS
import homestead_static as STATIC
ICONS.bind(DATA_DIR)
import homestead_volumes as VOLUMES
import homestead_smart as SMART
import homestead_shares as SHARES
import homestead_networking as NETWORK, homestead_vip_registry as VIP_REGISTRY, homestead_network_inventory as NET_INVENTORY, homestead_network_services as NET_SERVICES
import homestead_flow as FLOW
import homestead_firewall as FIREWALL
import homestead_routes as MODULE_ROUTES
MODULE_ROUTES.bind(lambda key, seconds, fn: CACHE.cached(key, seconds, fn), _cache)
import homestead_cluster as CLUSTER
import homestead_probe as PROBE
import homestead_objectstore as OBJECTS
import homestead_move as MOVE
import homestead_fleet as FLEET
import homestead_host_access as HOSTACCESS
import homestead_request_scope as REQUEST_SCOPE
HOSTACCESS.bind_targets(SYS_NS)
import homestead_signins as SIGNINS
import homestead_config_backup as CONFIG
import homestead_move_source as MOVE_SOURCE
import homestead_move_engine as MOVE_ENGINE
import homestead_deploy as DEPLOY, homestead_deploy_manifests as DEPLOY_MANIFESTS
import homestead_workloads as WORKLOADS
import homestead_onboard as ONBOARD
import homestead_cfaccess as CFACCESS
import homestead_push as PUSH
import homestead_alerts as ALERTS
import homestead_self as SELF
KUBE_HELPERS.FENCE_READY = True
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
import homestead_vms as VMS, homestead_vm_list as VM_LIST
import homestead_vm_review as VM_REVIEW
import homestead_isos as ISOS
import homestead_lhcapacity as LHCAP
import homestead_lhrebuild as LHREBUILD
import homestead_power_hold as HOLD
import homestead_rebalance as REBALANCE, homestead_copy_move as COPY_MOVE, homestead_host_move as HOST_MOVE
import homestead_container_rebalance as CREBALANCE
import homestead_disks as DISKS
import homestead_power as POWER
import homestead_privileges as PRIV
NAMES.bind(kget)
PROBE.bind(kget, ksend, DEFAULT_NS, AUTH.smart_signing_key, version=HOMESTEAD_VERSION)
ALLOCATION_PROBE.bind(kget, ksend, DEFAULT_NS, allocation_probe_capacity, version=HOMESTEAD_VERSION,
                      current_hosts=lambda: PLACE.get_nodes(), inspect=ALLOCATION_EVIDENCE.inspect)
OBJECTS.bind(kget, ksend, create_pvc, DEFAULT_NS)
MOVE.bind(kget, ksend, DEFAULT_NS, HOMESTEAD_VERSION)
FLEET.bind(kget, ksend, DEFAULT_NS, HOMESTEAD_VERSION,
           site=lambda: (CACHE.cached("settings", 15, SETTINGS_HELPERS.get_app_settings) or {}).get("site_name", ""),
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
        return VM_ROUTES.vm_default_class() or "longhorn-r2"
    except Exception:
        return "longhorn-r2"


STORAGE_CLASS = _resolve_storage_class()
LC.bind(kget, ksend, SYS_NS, _cache, HW.features, create_pvc, STORAGE_CLASS)
IMP.bind(kget, ksend, create_pvc, DEPLOY_MANIFESTS.build_deployment, DEFAULT_NS, _cache, HW.features)
IMP.SCAN_DIR = DATA_DIR       # each node's full image list, kept for every replica
AUTH.bind(kget, _auth_ksend, DEFAULT_NS)
CAPACITY_REVIEW.bind(AUTH.review_signing_key)
LH.bind(kget, ksend, _cache, STORAGE_CLASS, DEFAULT_NS)
PLACE.bind(kget, ksend, lambda: CACHE.cached("nodes", 5, NODES.get_nodes), _cache, HW.features)
POWER.bind(kget, PLACE.impact, NODE_ACTIONS.quorum_report, lambda: LC.NODE_POWER_ENABLED)
HOLD.bind(kget, ksend, (SELF.NS, NAMES.BRAND), POD_NAME)
# The helper image is read from Homestead's own pod, defined further down.
POWER_JOBS.bind(kget, ksend, lambda read, ns: SELF_DATA_ROUTES._self_data_helper_image(read, ns), homestead_running_on, POD_NAME)
REBALANCE.bind(kget, ksend, lambda: LEADER.is_leader())


CREBALANCE.bind(kget, PLACE.requirements, PLACE.satisfies, rebalance_move, (SELF.NS, NAMES.BRAND), lambda: LEADER.is_leader(),
                PLACE.get_nodes, HOSTACCESS.require_target_as, OPS, lambda: PLACEMODE.standby_hosts())
POWER.WORKER_GONE, POWER.RESUME = POWER_JOBS.power_worker_gone, POWER_JOBS.resume_power_job
POWER.RESTORE, POWER.UNCORDON = POWER_JOBS.restore_held, POWER_JOBS.allow_scheduling
UPDATES.bind(kget, ksend, DEFAULT_NS, DATA_DIR, SYS_NS, SMB_NAMESPACE,
             channel=lambda: CACHE.cached("settings", 15, SETTINGS_HELPERS.get_app_settings)["updates"]["channel"])
UPDATES.PART = homestead_part
UPDATES.VERSION = lambda: HOMESTEAD_VERSION
SMART.bind(kget, DEFAULT_NS, AUTH.smart_signing_key)
OPS.bind(kget, DATA_DIR, UPDATE_ROLLOUTS.progress, SMART.progress)
OPS.cdi_send = ksend
RESTRUCTURE.bind(kget, ksend, raw_get)
AFFINITY.bind(kget)
FAILOVER.bind(kget, ksend)
LAN.bind(kget, ksend)
PULLWATCH.bind(kget, ksend, raw_get, lambda image, arch: UPDATE_REGISTRY.image_layers(image, arch), DEFAULT_NS, DATA_DIR)


UPDATES.pull_progress = _pull_progress
K3SC.bind(kget, lambda cfg: create_vm_with_address(cfg), lambda ip: VM_ROUTES.vm_address_problem(ip), _remove_cluster_vm)


MOVE_ENGINE.after_finish = cleanup_restores


UPGRADES.bind(kget)
PORTAL.bind(kget, ksend, DEFAULT_NS, lambda: CACHE.cached("wl", 5, WORKLOADS.get_workloads),
            lambda source: ICONS.persist(source, DATA_DIR), lambda reference: ICONS.data_url(reference, DATA_DIR))
import homestead_reclass as RECLASS
import homestead_storage_admission as STORAGE_ADMISSION
import homestead_storage_recovery as STORAGE_RECOVERY
import homestead_storage_runtime as STORAGE_RUNTIME


def _bind_recovery():
    """The job tray's recovery reviews read through server.py's reader, so a
    test that patches it is the one they use."""
    OPS.bind_recovery(lambda *a, **k: KUBE_HELPERS.kget(*a, **k), lambda *a: STORAGE_ROUTES.storage_helper_admission(*a),
                      lambda *a: STORAGE_ROUTES.storage_restart_admission(*a), lambda: storage_runtime_check())


_bind_recovery()


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
import homestead_usage as USAGE
import homestead_requests as RESERVE
import homestead_recovery as RECOVERY
import homestead_priority as PRIORITY
import homestead_group_tiers as GROUPS
set_workload_groups = GROUPS.set_workload_groups   # server.py's name for it before #430
import homestead_placement_mode as PLACEMODE
import homestead_capacity as CAPACITY
import homestead_mcp as MCP
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
import homestead_disk_allocation as DISK_ALLOCATION
import homestead_longhorn_runtime as LH_RUNTIME
import homestead_hvimage as HVIMAGE
import homestead_revert as REVERT
MOVE_SOURCE.bind(kget, ksend, LH, DEFAULT_NS)
MOVE_ENGINE.bind(kget, ksend, LH, MOVE, NETWORK, OPS, DATA_DIR, DEFAULT_NS, guard=lambda ns, name: OVERVIEW_ROUTES.guard_managed_smb(ns, name))


WORKLOADS.bind(_late(*WORKLOADS.NEEDS), api=API, token=TOKEN, ssl_context=CTX, default_ns=DEFAULT_NS,
               data_dir=DATA_DIR, system_namespaces=SYS_NS, platform_namespaces=PLATFORM_NS)
GROUPS.bind(_late(*GROUPS.NEEDS), namespace=DEFAULT_NS, own_group=OWN_GROUP, managed=UPDATES.managed_by)
DEPLOY.bind(_late(*DEPLOY.NEEDS), default_ns=DEFAULT_NS, version=HOMESTEAD_VERSION)
UPDATES.bind_routes(_late(*UPDATES.ROUTE_NEEDS))
IMP.bind_routes({**_late("guard_managed_smb", "preview_import", "reviewed_import"),
                 "workload_service_names": lambda ns, name: NET_SERVICES.workload_service_names(ns, name)})
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


DIAGNOSTICS.bind(DATA_DIR, _diagnostic_read, OPS, HOMESTEAD_VERSION, _own_namespace(), os.environ.get("HOSTNAME", ""))
SHARED.bind(DATA_DIR)
LEADER.bind(kget, ksend, _own_namespace())
IPAM.bind(kget, ksend, DEFAULT_NS, lambda: CACHE.cached("network", 5, NET_INVENTORY.inventory))
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


RESOURCES.bind(kget, ksend, ktable, lambda kind, ns, name: guard_smb_object(kind, ns, name), DEFAULT_NS)
VMS.bind(kget, ksend, RESOURCES.events_for, lambda annotations: display_icon(annotations), DEFAULT_NS,
         lambda b: set_vm_logos(b), lambda b: set_vm_monitoring(b))
# Looked up as each call is made, so what a test puts in their place is used.
VM_REVIEW.bind(lambda *a, **k: KUBE_HELPERS.kget(*a, **k), lambda *a, **k: KUBE_HELPERS.ksend(*a, **k), DEFAULT_NS,
               lambda *a, **k: _dns_name(*a, **k), lambda current: rollout_review_context(current),
               lambda: SETTINGS_HELPERS.get_app_settings(), lambda *a: VM_ROUTES.vm_default_class(*a), lambda ip: VM_ROUTES.vm_address_problem(ip),
               lambda: vm_create_options())
VMUSAGE.bind(kget)
VMS.platform, VMS.images = PLATFORM.detect, VM_CREATE.list_vm_images
STORAGE_CLASSES.bind(kget, ksend, storage_classes, _dns_name, lambda: NODES.node_temps(), _cache, DATA_DIR, DEFAULT_NS,
                     selectable_storage_classes, shared_storage_classes, storage_class_facts)
STORAGE.bind(kget, ksend, storage_classes, create_pvc, age_secs, _cache, SYS_NS, DEFAULT_NS, storage_runtime_check)
LHCAP.bind(kget, ksend, STORAGE_CLASSES.v2_engine_status)
LHREBUILD.bind(kget, ksend, lambda: (SETTINGS_HELPERS.get_app_settings().get("longhorn") or {}).get("offline_rebuilding", True),
               settings=get_app_settings, save_settings=save_app_settings)
import homestead_lhv2_setup as LHV2_SETUP
LHV2_SETUP.bind(kget, ksend, STORAGE_CLASSES.v2_engine_status, OPS, DEFAULT_NS)
RECLASS.bind(kget, ksend, raw_get, storage_classes, LHCAP.status, _own_namespace())
REVERT.bind(kget, ksend, RECLASS, is_self)
NODESHELL.bind(kget, ksend, DEFAULT_NS)
HOSTRUN.bind(kget, ksend, lambda *a, **k: FILES._exec(*a, **k), DEFAULT_NS)
import homestead_host_console as HOST_CONSOLE
HOST_CONSOLE.bind(kget, HOSTRUN, OPS, HOMESTEAD_VERSION, DATA_DIR)
HOST_CONSOLE.platform = lambda: PLATFORM.detect()
import homestead_unraid_vms as UNRAID_VMS
UNRAID_VMS.bind(IMP, kget, ksend, OPS, lambda: PLATFORM.detect(), (storage_classes, LHCAP.capacity, vm_default_class))
# Each module registers its own kinds of job (#622).
for _module in (REBALANCE, COPY_MOVE, HOST_MOVE, CREBALANCE, VM_MUTATION_JOB, COMPONENTS, LH, MOVE_ENGINE, LHV2_SETUP, SHARES, UNRAID_VMS):
    _module.register(OPS)
MANIFESTS.bind(kget, HOSTRUN, PLATFORM.detect, DATA_DIR)
HOST_LIMITS.bind(kget, HOSTRUN, PLATFORM.detect, DATA_DIR)
NODE_PARITY.bind(kget, ksend, HOSTRUN, PLATFORM.detect, NODES.node_temps, DATA_DIR, (SELF.NS, NAMES.BRAND))
HOST_OS.bind(kget, HOSTRUN, PLATFORM.detect, DATA_DIR)
PORTS.bind(DATA_DIR)
UPTIME.bind(DATA_DIR, setting=lambda b: set_uptime_setting(b))
HOUSEKEEPING.bind(DATA_DIR, kget=lambda path: KUBE_HELPERS.kget(path), portal_links=lambda: PORTAL.stored())
SCHEDULES.bind(DATA_DIR, items=lambda: scheduled_items(), setter=lambda b: set_schedule(b))
OUTAGE.bind(DATA_DIR, kget=lambda path: KUBE_HELPERS.kget(path), ksend=lambda *a, **k: KUBE_HELPERS.ksend(*a, **k),
            dns_name=lambda value, label: _dns_name(value, label), guard=lambda ns, name: OVERVIEW_ROUTES.guard_managed_smb(ns, name), ns=DEFAULT_NS)
CHANGES.bind(DATA_DIR, plan=lambda b: change_undo_plan(b), ksend=lambda *a, **k: KUBE_HELPERS.ksend(*a, **k), is_leader=LEADER.is_leader, beat=beat,
             deployments=lambda: [d for d in KUBE_HELPERS.kget("/apis/apps/v1/deployments").get("items", []) if d["metadata"]["namespace"] not in SYS_NS])
FORECAST.bind(DATA_DIR, lambda: (CACHE.cached("vol", 8, STORAGE.get_volumes), LHCAP.status()), LEADER.is_leader, beat)
USAGE.bind(DATA_DIR, parse_cpu, parse_mem, lambda path, **options: KUBE_HELPERS.kget(path, **options), FLOW._flow_owner, SYS_NS, LEADER.is_leader, beat)
RESERVE.bind(kget, ksend, USAGE.summary, UPDATES.managed_by, (SELF.NS, NAMES.BRAND), SYS_NS, lambda: LEADER.is_leader(),
             HOSTACCESS.require_target_as, OPS)
PLACEMODE.bind(kget, ksend, lambda: CACHE.cached("settings", 15, SETTINGS_HELPERS.get_app_settings), SYS_NS)
CAPACITY.bind(kget, NODES.node_impact, NODES._impact_sources, FLOW._flow_owner, lambda pod: NAMES.label_of(pod["metadata"], "task"))
RECOVERY.bind(DATA_DIR, kget, lambda: CACHE.cached("settings", 15, SETTINGS_HELPERS.get_app_settings), OPS,
              lambda exclude=(), avoid=(): CREBALANCE.plan(exclude, avoid=avoid), CREBALANCE.start,
              (CREBALANCE.KIND, RESERVE.KIND, AUTOUPDATE.KIND, "os-rollout", "node-power", "k3s-cluster"), LEADER.is_leader, beat,
              UPDATES.managed_by)
PRIORITY.bind(kget, ksend, UPDATES.managed_by, (SELF.NS, NAMES.BRAND), SYS_NS, lambda: LEADER.is_leader(),
              HOSTACCESS.require_target_as, OPS)
MCP.bind(DATA_DIR, lambda: CACHE.cached("settings", 15, SETTINGS_HELPERS.get_app_settings), HOMESTEAD_VERSION, ROUTE_POLICY.role, lambda m, p, b: (
    KUBE_HELPERS.require_workload_target(b.get("ns") or DEFAULT_NS, b.get("name") or "") if p in ROUTE_POLICY.TARGET_CHECKED else None),
    kget, lambda ns, name: delete_workload(ns, name))


ROOT_GUARD.bind(kget, ksend, PLATFORM.detect, NODES.node_temps, DATA_DIR)
PASSTHROUGH.bind(kget, ksend, HOSTRUN, PLATFORM.detect, DATA_DIR)
SELF_ADDRESS.bind(kget, NETWORK, OBJECTS, SELF.NS, os.environ.get("PORT", "8080"), SMB_NAMESPACE, SMB_NAME,
                  follow_fleet=lambda: _follow_fleet_address())

BASELINE.vip_setup = installer_vip
OS_ROLLOUT.bind(kget, PLATFORM.detect, HOST_OS, POWER_JOBS.rollout_reboot, operation_item, NODE_ACTIONS.set_cordon, own_node, DATA_DIR,
                lambda rollout: OPS.start("os-rollout", f"Update every host's OS ({len(rollout['nodes'])} hosts)",
                                          {"kind": "Node", "name": ", ".join(rollout["nodes"])[:200]}, "/nodes",
                                          {"rollout": rollout["id"]},
                                          "In the weekly window" if rollout["reason"] == "schedule" else "Starting"),
                lambda node, since: POWER_JOBS.rollout_power_job(node, since))
DISK_SETUP.bind(HOSTRUN)
HOST_BRIDGE.bind(HOSTRUN, kget, ksend)
HOST_BONDS.bind(HOSTRUN, kget, ksend, PLATFORM.detect)

UPLINKS.bind(kget, ksend, lambda: NODES.node_temps())
# Each module registers its own kinds of job (#622).
for _module in (K3SC, POWER, HOST_CONSOLE, HOST_OS, OS_ROLLOUT, HOST_BRIDGE, HOST_BONDS, UPLINKS,
                VM_POWER_JOB, RENAME, IMPORT_JOB, AUTOUPDATE, RESTORE_TEST, DISK_V2, DISK_ALLOCATION, LH_RUNTIME, DISKS, HELM, ADDONS):
    _module.register(OPS)
RESTORE_TEST.bind(kget, ksend, NAMES, lambda volume=None, strict=False: LH_BACKUPS.backups(volume, strict), LH_RESTORE.restore_backup, OPS._volume_restore,
                  LH_RESTORE.cleanup_restore_snapshots, LHCAP.status, lambda target: UPTIME.ask_ports(target), lambda: SELF.NS,
                  lambda: CACHE.cached("wl", 5, WORKLOADS.get_workloads), get_app_settings, save_app_settings, _dns_name)

AUTOUPDATE.bind(kget, ksend, UPDATES, lambda: UPTIME.report(),
                lambda volume, name, annotations: LH_VOLUMES.create_snapshot(volume, name, annotations),
                lambda volume: LH_VOLUMES.snapshots(volume), _prune_snapshot, NAMES)


DISKS.setup_module = DISK_SETUP
DISK_SETUP.longhorn_block_paths = DISKS.longhorn_block_paths
PASSTHROUGH.longhorn_block_paths = DISKS.longhorn_block_paths
DISKS.autotag_state = lambda: os.path.join(DATA_DIR, "disk-autotags.json")


VMSTORE.bind(kget, ksend, DEFAULT_NS, PLATFORM.detect,
             lambda url, display, reuse: HVIMAGE.download(KUBE_HELPERS.kget, KUBE_HELPERS.ksend, DEFAULT_NS, url, "", None, display, reuse),
             lambda: VM_CREATE.list_vm_images(raw=True), _vm_image_disks,
             lambda ns, name: KUBE_HELPERS.ksend("DELETE", f"/apis/harvesterhci.io/v1beta1/namespaces/{ns}/virtualmachineimages/{name}"))
SNAPSHOT_DELETE.bind(kget, ksend)
DISKS.bind(kget, ksend, NODES.node_temps)
DISK_V2.bind(kget, ksend, PLATFORM.detect, OPS, DEFAULT_NS, _diagnostic_read)
DISK_V2.protect_mutations(DISKS)
DISK_ALLOCATION.bind(kget, ksend, PLATFORM.detect, OPS, DEFAULT_NS, _diagnostic_read)
DISK_ALLOCATION.protect_mutations(DISKS)
LH_RUNTIME.bind(OPS)
LH_RUNTIME.protect_mutations(DISKS)

# Every job can be cancelled; what that does for each kind is said there.
CANCEL.bind(kget, ksend, delete_workload, lambda: STORAGE_CLASSES.cleanup_restore_classes())
CANCEL.register(OPS)
# Storage, cluster and self-data register their own; storage replaces
# the reclass canceller registered just above.
for _module in (STORAGE_ROUTES, CLUSTER_ROUTES, SELF_DATA_ROUTES):
    _module.register(OPS)
# What each job has to show for itself: a pod's log, a VM's console.
JOBLOGS.bind(kget, raw_get)
JOBLOGS.register(OPS)
NSMOD.bind(kget, ksend, DEFAULT_NS, _own_namespace())
ALERTS.bind(DATA_DIR)


import homestead_storage_pending as STORAGE_PENDING
STORAGE_PENDING.bind(kget, ksend)


class MissingParameter(ValueError):
    """A route asked for without what it needs: 400, saying which."""


class Query(dict):
    """A request's query string. A route reading a parameter it was not
    given gets a 400 naming it, not a 500 from a bare KeyError."""
    def __missing__(self, key):
        raise MissingParameter(f"missing parameter: {key}")


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
                    job = next((i for i in OPS.stored() if i.get("kind") == SELF_DATA_EXECUTE.KIND and i.get("ref", {}).get("operation") == operation), None)
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
            job = next((i for i in OPS.stored() if i.get("kind") == SELF_DATA_EXECUTE.KIND and i.get("ref", {}).get("operation") == operation), None)
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
                anchor = SELF_DATA_FENCE.A.Anchor(KUBE_HELPERS.kget, None, SELF.NS, NAMES.BRAND)
                anchor.load(operation=operation, uid=ref["anchor_uid"])
                return SELF_DATA_WORKER.progress(anchor, time.time())
        if not marker or (marker["namespace"], marker["deployment"], marker["operation"]) != (SELF.NS, NAMES.BRAND, operation):
            return None
        anchor = SELF_DATA_FENCE.A.Anchor(KUBE_HELPERS.kget, None, SELF.NS, NAMES.BRAND)
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
    anchor = SELF_DATA_FENCE.A.Anchor(KUBE_HELPERS.kget, KUBE_HELPERS._ksend, SELF.NS, NAMES.BRAND).load(operation=recovery["operation"], uid=recovery["anchor_uid"])
    if not anchor.state.get("setup_aborted"):
        anchor.abort_setup()  # CAS disarms the publisher; never clears a published handoff.
    if not _self_data_boot_pending:
        threading.Thread(target=finish_self_data_helpers, name="data-move-cleanup", daemon=True).start()
    return {"ok": True, "message": "Preparation abandoned. Both volumes are retained."}


CLUSTER_SHUTDOWN.bind(cluster_shutdown)


def start_self_data_move(body, actor):
    """Persist the approved intent before launching the one-shot source setup."""
    if _self_data_barrier is None:
        raise SELF_DATA_FENCE.Held("Data moves require the mounted-data safety guard")
    ns = SELF.NS
    def reviewer(read):
        pod, image = SELF_DATA_ROUTES._self_data_helper_image(read, ns)
        def runtimes(pods, claim):
            return STORAGE_RUNTIME.require_self_data(OPS, pods, ns, NAMES.BRAND, HOMESTEAD_VERSION, claim,
                own_uid=pod["metadata"]["uid"], data_mount=DATA_DIR)
        return SELF_DATA_REVIEW.Review(read, ns, NAMES.BRAND, actor=actor, image=image,
            threshold=SETTINGS_HELPERS.get_app_settings()["thresholds"]["memory"]["critical"], source_pod=pod, data_dir=DATA_DIR,
            runtime_check=runtimes, clock=time.time, route_check=SELF_DATA_ROUTE.review)
    with OPS.transaction():
        SELF_DATA_ROUTES._require_no_data_handoff(KUBE_HELPERS.kget, ns)
        SELF_DATA_EXECUTE.idle(OPS)
        execution = reviewer(KUBE_HELPERS.kget).approve(body)
        job = SELF_DATA_EXECUTE.enqueue(OPS, execution)
    def recheck(approved, worker):
        def read(path):
            result = KUBE_HELPERS.kget(path)
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
    task = SELF_DATA_EXECUTE.Execution(KUBE_HELPERS.kget, KUBE_HELPERS._ksend, OPS, execution, job, directory=DATA_DIR,
        barrier=_self_data_barrier, recheck=recheck)
    try:
        threading.Thread(target=task.run, name="self-data-setup", daemon=True).start()
    except Exception:
        task.checkpoint(setup_error="The move setup could not start. Inspect the saved job before trying again.")
        raise SELF_DATA_FENCE.Held("Move setup could not start; no data was copied or switched") from None
    return {"ok": True, "operation": job, "handoff": execution["scope"].operation}, execution["status_token"]


# Looked up when asked, so a test that patches one is the one used.
SELF_HEALTH.bind(lambda *a, **k: KUBE_HELPERS.kget(*a, **k), lambda *a, **k: KUBE_HELPERS.ksend(*a, **k),
                 lambda dep=None: SELF_DATA_ROUTES.homestead_data_volume(dep), _heartbeats,
                 lambda: NODES.node_temps(), lambda: SHARE_SERVERS.samba_state(), DEFAULT_NS, LB_IP, HOMESTEAD_VERSION)
VOLUMES.bind(kget, ksend, LH_VOLUMES.snapshots, LH_BACKUPS.backups, _cache, SYS_NS, DEFAULT_NS)
SHARES.bind(kget, ksend, create_pvc, SMB_NAMESPACE, _cache)
SHARES.install = SHARE_SERVERS.install_samba
SHARE_SERVERS.bind(kget, ksend, lambda cfg: DEPLOY_MANIFESTS.build_deployment(cfg), SMB_NAMESPACE, _cache,
                   shared_storage_classes, lambda: STORAGE_CLASS, share_storage_options)
NETWORK.bind(kget, ksend, SYS_NS, DEFAULT_NS, LB_IP, lambda ns, name: OVERVIEW_ROUTES.guard_managed_smb(ns, name), lambda: NODES.node_temps())
FLOW.bind(lambda path, **options: KUBE_HELPERS.kget(path, **options), SYS_NS, lambda annotations: display_icon(annotations), STORAGE._moving_copy,
          parse_cpu, parse_mem, age_secs)
FIREWALL.bind(kget, ksend, PLATFORM.detect, _own_namespace())
VIPS.bind(kget, ksend)


SIGNINS.bind(DATA_DIR)
import homestead_api_keys as API_KEYS
import homestead_api_v1 as API_V1
import homestead_setup as SETUP
import homestead_setup_code as SETUP_CODE
import homestead_auth_routes as AUTH_ROUTES
AUTH_ROUTES.bind(DEFAULT_NS)
API_KEYS.bind(DATA_DIR)
API_KEYS.CLIENTADDR.bind(lambda path: KUBE_HELPERS.kget(path))
# Looked up when asked, so a test that patches one is the one the guide reads.
SETUP.bind(DATA_DIR, overview=lambda: OVERVIEW_ROUTES.get_overview(), nodes=lambda: NODES.get_nodes(), workloads=lambda: WORKLOADS.get_workloads(),
           vm_networks=lambda: vm_network_details(strict=True), storage_classes=lambda: storage_classes(),
           samba=lambda: SHARE_SERVERS.samba_state())
API_V1.bind(nodes=lambda: CACHE.cached("nodes", 5, NODES.get_nodes), workloads=lambda: CACHE.cached("wl", 5, WORKLOADS.get_workloads),
            vms=lambda: CACHE.cached("vms", 5, VM_LIST.list_vms),
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
     "objects": [("configmaps", DEFAULT_NS, VIP_REGISTRY.VIP_MAP)]},
    {"id": "ipam", "label": "IP addresses", "detail": "Subnets, documented addresses, and the UniFi connection",
     "objects": [("configmaps", DEFAULT_NS, IPAM._map()), ("secrets", DEFAULT_NS, IPAM._secret())]},
    {"id": "mqtt", "label": "MQTT", "detail": "The broker, its credentials and what is published",
     "objects": [("configmaps", DEFAULT_NS, MQTT._map()), ("secrets", DEFAULT_NS, MQTT._secret())]},
    {"id": "portal", "label": "Portal", "detail": "Its sections and tiles",
     "objects": [("configmaps", DEFAULT_NS, PORTAL._map())]},
    {"id": "group-tiers", "label": "Group tiers", "detail": "The priority tier each Containers group gives the apps that follow it",
     "objects": [("configmaps", DEFAULT_NS, GROUPS._map())]},
    {"id": "shares", "label": "Network shares", "detail": "Shares, their options, and SMB users",
     "caution": "Brings back share definitions and SMB users; the volumes they point at must still exist.",
     "objects": [("configmaps", SMB_NAMESPACE, SHARES.CONFIGMAP()), ("secrets", SMB_NAMESPACE, SHARES.SECRET())]},
    {"id": "sources", "label": "Import sources", "detail": "Unraid and Docker hosts to import from",
     "objects": [("configmaps", DEFAULT_NS, IMP._sources_map())]},
    {"id": "vmstore", "label": "VM image store", "detail": "The cloud images kept, and whether they refresh",
     "objects": [("configmaps", DEFAULT_NS, VMSTORE.CONFIGMAP)]},
], site=lambda: (CACHE.cached("settings", 15, SETTINGS_HELPERS.get_app_settings) or {}).get("site_name", ""),
    after_restore=_config_restored)
CLUSTER.bind(kget, SYS_NS, lambda: CACHE.cached("nodes", 5, NODES.get_nodes), lambda: OVERVIEW_ROUTES.get_overview())
CONSOLE_PROXY = CONSOLE.ConsoleProxy(API, TOKEN, CTX, DATA_DIR, SYS_NS, {DEFAULT_NS}, kget)
VM_CONSOLE = VMCONSOLE.VmConsole(CONSOLE_PROXY, SYS_NS, kget)
FILES.bind(kget, ksend, urllib.parse.urlparse(API), TOKEN, CTX, SYS_NS)
SNAPSHOT_FILES.bind(kget, ksend, lambda: SELF_DATA_ROUTES._self_data_helper_image(KUBE_HELPERS.kget, SELF.NS)[1], SYS_NS)


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
                KUBE_HELPERS.ksend("POST", "/apis/storage.k8s.io/v1/storageclasses", body)
            except urllib.error.HTTPError as error:
                if error.code != 409:      # another replica made it first
                    raise
        return ISO_CLASS, True
    if base:
        return base, True
    return VM_ROUTES.vm_default_class(rows), False


ISOS.bind(kget, ksend, SHARES.list_shares, FILES.list_files, iso_storage_class, SHARES._samba_node,
          SMB_NAMESPACE, DEFAULT_NS, FILES.IMAGE)
VMS.iso_ready = ISOS.ready_volume
IMP.iso_ready = ISOS.ready_volume


# The largest honest request is a 1 MiB file edit, JSON-encoded.
MAX_BODY = 8 * 1024 * 1024

# What every response says about how it may be used. Inline handlers are how
# the pages are written, so scripts may be inline - but only from here: no
# script, frame or form from elsewhere, and no framing of Homestead at all.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "img-src 'self' data: blob: https:",
    "connect-src 'self'",
    "worker-src 'self' blob:",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'self'",
    "object-src 'none'",
])


# Policies are explicitly declared in homestead_route_policy.py. These sets
# document the sensitive routes and the user-owned account routes.
# Enforced here, server-side. The UI hides what you cannot do as a courtesy,
# but a viewer who hand-crafts the request still gets a 403.
ADMIN_ROUTES = {
    "/api/disks/v2/plan", "/api/disks/v2/start", "/api/disks/v2/status",
    "/api/disks/v2/prepare-review", "/api/disks/v2/prepare",
    "/api/longhorn/v2/plan", "/api/longhorn/v2/prepare", "/api/longhorn/v2/enable",
    # The setup guide's checks that reach out, and its first opening.
    "/api/setup/https-check", "/api/setup/opened",
    # Who signed in, from where: other people's addresses and devices.
    "/api/auth/history",
    "/api/node/power", "/api/node/drain", "/api/node/cordon", "/api/node/hardware",
    "/api/sources", "/api/sources/delete", "/api/sources/browse",
    "/api/sources/scan", "/api/sources/trust",
    "/api/sources/containers", "/api/sources/inspect", "/api/sources/measure",
    # A server's VMs, one shut down there, and one copied across.
    "/api/sources/vms", "/api/sources/vms/shutdown", "/api/vms/import-unraid", "/api/vms/import-unraid/room",
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
    "/api/cluster/components/upgrade", "/api/cluster/components/manage-operator", "/api/cluster/upgrades/start",
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


# ---------------------------------------------------------------- HTTP
_key_refusals = {}      # address -> when a refused API key was last written down


class H(HTTP.LimitedHandler):
    protocol_version = "HTTP/1.1"
    timeout = HTTP.IDLE_SECONDS
    max_body = MAX_BODY
    after_request = MODULE_ROUTES.after_request

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
        if HTTP.closes_after(self, code):
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
        return STATIC.serve(self, path, ctype, cache, HOMESTEAD_VERSION)

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

    def _set_cookie(self, token, max_age=None):
        # Secure only behind TLS: Homestead is usually served over plain HTTP
        # on a LAN address, and a Secure cookie there would never be sent back.
        self._extra_headers.append(("Set-Cookie", MODULE_ROUTES.cookie_header(MODULE_ROUTES.Cookie(
            AUTH.COOKIE, token, max_age=AUTH.SESSION_TTL if max_age is None else max_age), self._over_tls())))

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
                self._access = CFACCESS.verify(token)
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
                self._send(503, WEB_PATHS.handoff_body(path, {"error": str(error), "data_handoff": True}))
                return True
            # Ready means the real app loaded and can read its copied journal
            # and account key, not that normal writers have been released.
            if self.command == "GET" and path in ("/healthz", "/readyz"):
                self._send(200, WEB_PATHS.handoff_body(path, {"ok": True, "data_handoff": True, "read_only": True}))
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
        if path != "/api/self/data/abandon" and (self.command in ("POST", "PUT", "PATCH", "DELETE") or path in ("/healthz", "/readyz", "/api/auth/state", "/api/console", "/api/node/shell", "/api/vm/console")):
            try:
                require_self_data_write()
            except SELF_DATA_FENCE.Held as error:
                if self.command == "GET" and path in ("/healthz", "/readyz", "/api/auth/state") and _self_data_fence is not None:
                    try:
                        recovery = _self_data_fence.recovery()
                        self._send(200, WEB_PATHS.handoff_body(path, {"ok": True, "read_only": True, "data_handoff": True, "setup": False,
                                         "recovery": True, "operation": recovery["operation"]}))
                        return True
                    except Exception:
                        pass
                self._send(503, WEB_PATHS.handoff_body(path, {"error": str(error), "data_handoff": True}))
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
        """An API key: for /api/v1, /mcp and /metrics (every key reads), checked,
        and let through with its scopes, which /api/v1 enforces per endpoint. No cookie
        is involved, so there is no cross-site form to guard against."""
        if not path.startswith("/api/v1/") and path not in (MCP.PATH, "/metrics"):
            self._send(401, {"error": "API keys work only on /api/v1, /mcp and /metrics; GET /api/v1/openapi.json lists what they can do"})
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
        self.role = "operator" if any(scope.endswith((":control", ":manage")) for scope in key["scopes"]) else "viewer"
        HOSTACCESS.set_role(self.role)
        return None

    def _module_route(self, method, path, query, body):
        """A route a feature module declares (homestead_routes.py), answered;
        False when server.py answers it itself. The guard has already
        checked the role the module declared for it."""
        route = MODULE_ROUTES.find(method, path)
        if route is not None:
            MODULE_ROUTES.answer(self, route.handler(MODULE_ROUTES.Request(
                method, path, query, body, getattr(self, "user", None), getattr(self, "role", None),
                self._client_ip() if getattr(self, "client_address", None) else "", MODULE_ROUTES.connection(self))), route.module)
        return route is not None

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
        REQUEST_SCOPE.unbind()
        self.api_key = None
        self._extra_headers = []
        self._raw = None
        self._fleet_who = False
        self._fleet_from = self._access = None    # who the guard finds, this request's alone
        self._diagnostic_authorized = False
        self._diagnostic_started = time.monotonic()

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
        if FLEET.signed(headers) or FLEET.local_path(path) or path.startswith("/api/v1/") or path == MCP.PATH:
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
                self._extra_headers.append(("Set-Cookie", MODULE_ROUTES.cookie_header(
                    MODULE_ROUTES.Cookie(FLEET.COOKIE, "", max_age=0), self._over_tls())))
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

    @REQUEST_SCOPE.unbinds_after(self_data_request)
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
            if p == MCP.PATH:       # no event stream: one JSON answer per POST
                return self._send(405 if MCP.enabled() else 404, {"error": "POST JSON-RPC to /mcp"})
            if self._module_route("GET", p, q, None):
                return
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

    @REQUEST_SCOPE.unbinds_after(self_data_request)
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
            if p == MCP.PATH:
                return MCP.reply(self._send, self._raw_body(), self.api_key and {**self.api_key, "role": self.role}, self._client_ip(), self.headers)
            if int(self.headers.get("Content-Length") or 0) > MAX_BODY:
                return self._send(413, {"error": "that request is larger than Homestead accepts"})
            b = self._body()
            if not isinstance(b, dict):
                return self._send(400, {"error": "a JSON object is required"})
            if p in ROUTE_POLICY.TARGET_CHECKED:
                KUBE_HELPERS.require_workload_target(b.get("ns") or DEFAULT_NS, b.get("name") or "")
            if self._module_route("POST", p, Query(urllib.parse.parse_qs(u.query)), b):
                return
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

    @REQUEST_SCOPE.unbinds_after(self_data_request)
    def do_DELETE(self):
        self._begin()
        u = urllib.parse.urlparse(self.path)
        target = self._fleet_target(u.path)
        if target:
            return self._fleet_forward(target, u.path)
        try:
            if not self._guard(u.path) and not self._module_route("DELETE", u.path, Query(urllib.parse.parse_qs(u.query)), None):
                self._send(404, {"error": "no route"})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except AUTH.StoreUnavailable as e:
            # Not an empty account store: the cluster did not answer.
            return self._send(503, {"error": str(e), "cause": getattr(e, "cause", ""), "unavailable": True})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            return self._send(e.code, {"error": API_ERRORS.message(e, 500)})
        except Exception as e:
            return self._send(500, {"error": str(e)})


def start_background_tasks():
    """Start only after normal boot or the independent move's verified completion."""
    require_self_data_write()
    threading.Thread(target=DIAGNOSTICS.run, name="diagnostics-expiry", daemon=True).start()
    threading.Thread(target=SETUP_CODE.run, name="setup-code", daemon=True).start()
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
    threading.Thread(target=_jobs_loop, daemon=True).start()
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
    threading.Thread(target=CHANGES.run, name="changes", daemon=True).start()
    threading.Thread(target=FORECAST.run, name="forecast", daemon=True).start()
    threading.Thread(target=USAGE.run, name="usage", daemon=True).start()
    threading.Thread(target=RECOVERY.run, name="recovery", daemon=True).start()
    threading.Thread(target=PRIORITY.run, name="priority", daemon=True).start()
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
                result = SELF_DATA_FINISH.finish(_self_data_fence, KUBE_HELPERS.kget, KUBE_HELPERS.ksend)
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


# Names tests still patch on server; track 2 moves their tests.
# End of the names tests still patch on server.


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
